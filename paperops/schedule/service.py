"""Automatic reel change scheduling.

A winder shaft ("spool") moves through waiting -> reserved -> active -> doffed.
Empty spools queue FIFO; the head of the queue is reserved for the next change.
The active spool reports its growing diameter and a change arms itself once the
target diameter is reached, so turn-ups no longer depend on an operator watching
the clock.

A change job reuses the reel transfer procedure (new_spool, retract, bite). Until
the new spool is turned up the job is still reversible: it can be cancelled (the
reserved shaft goes back to the head of the queue) or deferred. A web break marks
the running job interrupted; after the broke latch clears and the web is taken
back the same job resumes from exactly the stage it reached. Once the new spool is
turned up the cut is committed, so cancellation is refused and only resume is
possible.

Every movement is published on the event bus and therefore lands in the audit
stream, keyed by spool id and job id with the tick at which it happened.
"""

from typing import Any, Protocol

from ..audit import AuditService
from ..clock import IdSequence, LogicalClock
from ..errors import OutOfRangeError, PreGateError, UnknownKeyError
from ..event import EventBus, topics
from ..reel import ReelService
from ..store import Journal, keys
from .view import (
    JOB_CANCELLED,
    JOB_DEFERRED,
    JOB_DONE,
    JOB_INTERRUPTED,
    JOB_RUNNING,
    POINT_OF_NO_RETURN,
    SPOOL_ACTIVE,
    SPOOL_DOFFED,
    SPOOL_INTERRUPTED,
    SPOOL_RESERVED,
    SPOOL_WAITING,
    ScheduleView,
)

# Stages carried on a change job before and while the reel procedure is open.
STAGE_ARMED = "armed"
STAGE_COMPLETE = "complete"


class CalSource(Protocol):
    """Anything that can report whether the calender nip is loaded."""

    def nip_closed(self) -> bool: ...


class BrokeSource(Protocol):
    """The broke handling latch the schedule has to wait on."""

    def latched(self) -> bool: ...


class ScheduleService:
    """Spool queue, diameter-triggered change jobs and break recovery."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        reel: ReelService,
        cal: CalSource,
        broke: BrokeSource,
        audit: AuditService,
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._reel = reel
        self._cal = cal
        self._broke = broke
        self._audit = audit
        self._ids = IdSequence("job")
        self._spools: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._active = ""
        self._job: dict[str, Any] | None = None

    # ----------------------------------------------------------------- wiring

    def wire(self) -> None:
        """React to the broke latch: break interrupts, takeover releases."""
        self._bus.subscribe(topics.TOPIC_BROKE_LATCH, self._on_broke_latch)

    # ---------------------------------------------------------------- reading

    def view(self) -> ScheduleView:
        return ScheduleView(
            spools=tuple(dict(spool) for spool in self._spools.values()),
            queue=tuple(self._order),
            active=self._active,
            job=dict(self._job) if self._job is not None else None,
            broke_latched=self._broke.latched(),
            tick=self._clock.tick,
        )

    def spool(self, spool_id: str) -> dict[str, Any]:
        return dict(self._require_spool(spool_id))

    def open_job(self) -> dict[str, Any] | None:
        return dict(self._job) if self._job is not None else None

    def spool_trace(self, spool_id: str) -> list[dict[str, Any]]:
        """Every schedule event that touched one shaft, oldest first."""
        self._require_spool(spool_id)
        rows = []
        for record in self._audit.records():
            if not record.topic.startswith("schedule."):
                continue
            payload = record.payload if isinstance(record.payload, dict) else {}
            touched = _payload_str(payload, "spool_id") == spool_id
            # Diameter readings belong to the winding shaft only; they carry the
            # triggered job id but must not be attributed to that job's successor.
            if record.topic != topics.TOPIC_SCHEDULE_MEASURE:
                touched = touched or _payload_str(payload, "successor_id") == spool_id
            if touched:
                rows.append(self._trace_row(record))
        return rows

    def job_trace(self, job_id: str) -> list[dict[str, Any]]:
        """Every schedule event that belonged to one change job, oldest first."""
        rows = []
        for record in self._audit.records():
            if not record.topic.startswith("schedule."):
                continue
            payload = record.payload if isinstance(record.payload, dict) else {}
            if _payload_str(payload, "job_id") == job_id:
                rows.append(self._trace_row(record))
        if not rows:
            raise UnknownKeyError(f"no change job {job_id} on record")
        return rows

    # ------------------------------------------------------------- spool side

    def enlist(self, spool_id: str, target: float = 0.0) -> dict[str, Any]:
        """Put a fresh empty shaft at the back of the waiting queue."""
        if not spool_id:
            raise ValueError("spool_id is required")
        if spool_id in self._spools:
            raise PreGateError(f"schedule: spool {spool_id} is already in the line")
        if target < 0:
            raise OutOfRangeError("schedule: target diameter must not be negative")
        self._spools[spool_id] = {
            "spool_id": spool_id,
            "state": SPOOL_WAITING,
            "target": float(target),
            "diameter": None,
            "growth": None,
            "eta": None,
            "enlist_tick": self._clock.tick,
            "last_tick": None,
        }
        self._order.append(spool_id)
        self._publish(
            topics.TOPIC_SCHEDULE_SPOOL_ENLISTED,
            {"spool_id": spool_id, "target": float(target), "position": len(self._order) - 1},
        )
        self._persist()
        return self.spool(spool_id)

    def reorder(self, spool_id: str, position: int) -> dict[str, Any]:
        """Move a waiting shaft to another queue position (0 is next on the machine)."""
        spool = self._require_spool(spool_id)
        if spool["state"] != SPOOL_WAITING:
            raise PreGateError(f"schedule: spool {spool_id} is not waiting in the queue")
        if position < 0 or position >= len(self._order):
            raise OutOfRangeError(
                f"schedule: queue position {position} is outside 0..{len(self._order) - 1}"
            )
        self._order.remove(spool_id)
        self._order.insert(position, spool_id)
        self._publish(
            topics.TOPIC_SCHEDULE_SPOOL_REORDERED,
            {"spool_id": spool_id, "position": position, "queue": list(self._order)},
        )
        self._persist()
        return self.spool(spool_id)

    def mount(self, spool_id: str = "") -> dict[str, Any]:
        """Put the first empty shaft (or a named waiting one) on the machine."""
        if self._active:
            raise PreGateError(f"schedule: spool {self._active} is already winding")
        if self._job is not None:
            raise PreGateError("schedule: finish the open change job before mounting")
        if spool_id:
            spool = self._require_spool(spool_id)
            if spool["state"] != SPOOL_WAITING:
                raise PreGateError(f"schedule: spool {spool_id} is not waiting to mount")
            chosen = spool_id
        else:
            if not self._order:
                raise PreGateError("schedule: no empty spool queued")
            chosen = self._order[0]
        self._order.remove(chosen)
        self._spools[chosen]["state"] = SPOOL_ACTIVE
        self._active = chosen
        self._publish(
            topics.TOPIC_SCHEDULE_SPOOL_ACTIVE,
            {"spool_id": chosen, "reason": "mount"},
        )
        self._persist()
        return self.spool(chosen)

    def measure(self, spool_id: str = "", diameter: float = 0.0, target: float = 0.0) -> dict[str, Any]:
        """Report a measured diameter; arms a change once the target is reached."""
        if diameter <= 0:
            raise ValueError("schedule: diameter has to be positive")
        spool = self._require_spool(spool_id or self._active)
        if spool["spool_id"] != self._active:
            raise PreGateError(f"schedule: spool {spool['spool_id']} is not the winding shaft")
        if spool["state"] != SPOOL_ACTIVE:
            raise PreGateError(
                f"schedule: spool {spool['spool_id']} is {spool['state']}, not winding"
            )
        if self._job is not None and self._job["status"] == JOB_RUNNING:
            raise PreGateError(
                f"schedule: change job {self._job['job_id']} is already running"
            )

        if target:
            spool["target"] = float(target)
        wanted = float(spool["target"] or 0.0)
        growth = spool["growth"]
        eta = spool["eta"]
        last_tick = spool["last_tick"]
        if last_tick is not None and self._clock.tick > last_tick:
            growth = (float(diameter) - float(spool["diameter"])) / (
                self._clock.tick - last_tick
            )
            if growth > 0 and wanted:
                eta = max(0.0, (wanted - float(diameter)) / growth)
        spool["diameter"] = float(diameter)
        spool["growth"] = growth
        spool["eta"] = eta
        spool["last_tick"] = self._clock.tick

        triggered = False
        job_id = ""
        if wanted and float(diameter) >= wanted:
            if self._job is None:
                if self._broke.latched():
                    raise PreGateError("schedule: the broke latch is set; clear it before turning up")
                job_id = self._arm_job(spool["spool_id"], "diameter", f"diameter {diameter:g}>={wanted:g}")
                triggered = True
            else:
                # An interrupted/deferred job is still open; tag the reading to it
                # but never arm a second job on top.
                job_id = str(self._job["job_id"])
        elif self._job is not None and self._job["spool_id"] == spool["spool_id"]:
            job_id = str(self._job["job_id"])
        self._publish(
            topics.TOPIC_SCHEDULE_MEASURE,
            {
                "spool_id": spool["spool_id"],
                "diameter": float(diameter),
                "target": wanted,
                "growth": growth,
                "eta": eta,
                "triggered": triggered,
                "job_id": job_id,
            },
        )
        self._persist()
        return {
            "spool_id": spool["spool_id"],
            "diameter": float(diameter),
            "target": wanted,
            "growth": growth,
            "eta": eta,
            "triggered": triggered,
            "job_id": job_id,
        }

    # --------------------------------------------------------------- job side

    def trigger(self, spool_id: str = "", reason: str = "manual turn up") -> dict[str, Any]:
        """Manually arm a change for the active shaft (or a named one)."""
        if self._broke.latched():
            raise PreGateError("schedule: the broke latch is set; clear it before turning up")
        if self._job is not None:
            raise PreGateError(f"schedule: change job {self._job['job_id']} is already open")
        chosen = spool_id or self._active
        spool = self._require_spool(chosen)
        if spool["state"] != SPOOL_ACTIVE:
            raise PreGateError(f"schedule: spool {chosen} is {spool['state']}, not winding")
        job_id = self._arm_job(chosen, "manual", reason)
        self._persist()
        return dict(self._job)

    def advance(self, job_id: str = "") -> dict[str, Any]:
        """Close the next stage of the reel transfer for the open change job."""
        job = self._require_open_job(job_id)
        if job["status"] != JOB_RUNNING:
            raise PreGateError(
                f"schedule: job {job['job_id']} is {job['status']}; resume it before advancing"
            )
        if self._broke.latched():
            raise PreGateError("schedule: the broke latch is set; clear it before turning up")
        next_stage = self._reel.procedure()["next"]
        if next_stage == "complete":
            raise PreGateError(f"schedule: job {job['job_id']} has no open stage left")

        if next_stage == "new_spool":
            successor = self._require_reserved_successor(job)
            self._reel.new_spool(successor, self._cal)
            job["stage"] = "retract"
            self._publish_step(job, "new_spool", {"successor_id": successor})
        elif next_stage == "retract":
            self._reel.retract_old()
            job["stage"] = "bite"
            old = job["spool_id"]
            successor = self._require_reserved_successor(job)
            self._spools[old]["state"] = SPOOL_DOFFED
            self._spools[successor]["state"] = SPOOL_ACTIVE
            self._active = successor
            self._publish(
                topics.TOPIC_SCHEDULE_SPOOL_DOFFED,
                {"spool_id": old, "job_id": job["job_id"], "tick": self._clock.tick},
            )
            self._publish(
                topics.TOPIC_SCHEDULE_SPOOL_ACTIVE,
                {
                    "spool_id": successor,
                    "job_id": job["job_id"],
                    "reason": "turn_up",
                },
            )
            self._publish_step(job, "retract", {"successor_id": successor})
        else:  # bite
            self._reel.bite()
            job["stage"] = STAGE_COMPLETE
            job["status"] = JOB_DONE
            self._publish_step(job, "bite", {"successor_id": job["successor_id"]})
            self._publish(
                topics.TOPIC_SCHEDULE_JOB_DONE,
                {
                    "job_id": job["job_id"],
                    "spool_id": job["spool_id"],
                    "successor_id": job["successor_id"],
                    "tick": self._clock.tick,
                },
            )
            self._job = None
        self._persist()
        return dict(job) if self._job is not None else {"job_id": job["job_id"], "status": JOB_DONE}

    def cancel(self, job_id: str = "") -> dict[str, Any]:
        """Call off a change that has not yet turned up the new spool."""
        job = self._require_open_job(job_id)
        self._require_reversible(job, "cancel")
        successor = job["successor_id"]
        if successor:
            self._release_successor(successor)
        job["status"] = JOB_CANCELLED
        job["stage"] = STAGE_COMPLETE
        self._reel.abort_change()
        self._publish(
            topics.TOPIC_SCHEDULE_JOB_CANCELLED,
            {
                "job_id": job["job_id"],
                "spool_id": job["spool_id"],
                "successor_id": successor,
                "tick": self._clock.tick,
            },
        )
        self._job = None
        self._persist()
        return {"job_id": job["job_id"], "status": JOB_CANCELLED}

    def defer(self, job_id: str = "") -> dict[str, Any]:
        """Park a change that has not yet turned up; resume it after the break."""
        job = self._require_open_job(job_id)
        self._require_reversible(job, "defer")
        successor = job["successor_id"]
        if successor:
            self._release_successor(successor)
        job["status"] = JOB_DEFERRED
        self._reel.abort_change()
        self._publish(
            topics.TOPIC_SCHEDULE_JOB_DEFERRED,
            {
                "job_id": job["job_id"],
                "spool_id": job["spool_id"],
                "successor_id": successor,
                "tick": self._clock.tick,
            },
        )
        self._persist()
        return dict(job)

    def resume(self, job_id: str = "") -> dict[str, Any]:
        """Take an interrupted or deferred change back to running after broke handling."""
        job = self._require_open_job(job_id)
        if job["status"] == JOB_RUNNING:
            raise PreGateError(f"schedule: job {job['job_id']} is already running")
        if self._broke.latched():
            raise PreGateError("schedule: process the broke and take the web back before resuming")
        if job["status"] == JOB_DEFERRED:
            successor = job["successor_id"]
            if not successor:
                raise PreGateError(f"schedule: deferred job {job['job_id']} lost its successor")
            spool = self._spools.get(successor)
            if spool is None or spool["state"] != SPOOL_WAITING or not self._order or self._order[0] != successor:
                raise PreGateError(
                    f"schedule: successor {successor} is no longer next in the queue"
                )
            self._order.remove(successor)
            spool["state"] = SPOOL_RESERVED
            self._publish(
                topics.TOPIC_SCHEDULE_SPOOL_RESERVED,
                {
                    "spool_id": successor,
                    "job_id": job["job_id"],
                    "reason": "resume",
                },
            )
            self._reel.begin_change("resumed turn up")
        job["status"] = JOB_RUNNING
        if self._spools[job["spool_id"]]["state"] == SPOOL_INTERRUPTED:
            self._spools[job["spool_id"]]["state"] = SPOOL_ACTIVE
        self._publish(
            topics.TOPIC_SCHEDULE_JOB_RESUMED,
            {
                "job_id": job["job_id"],
                "spool_id": job["spool_id"],
                "successor_id": job["successor_id"],
                "stage": job["stage"],
                "tick": self._clock.tick,
            },
        )
        self._persist()
        return dict(job)

    # ------------------------------------------------------------- replay

    def reload(self) -> ScheduleView:
        stored = self._journal.read(keys.SCHEDULE_STATE)
        if stored is None:
            return self.view()
        self._spools = {str(k): dict(v) for k, v in stored["spools"].items()}
        self._order = list(stored["order"])
        self._active = str(stored.get("active", ""))
        self._job = dict(stored["job"]) if stored.get("job") else None
        self._ids.restore(int(stored.get("job_counter", 0)))
        return self.view()

    # ------------------------------------------------------------ internals

    def _arm_job(self, spool_id: str, source: str, reason: str) -> str:
        successor = self._order[0] if self._order else ""
        job_id = self._ids.next()
        self._job = {
            "job_id": job_id,
            "spool_id": spool_id,
            "successor_id": successor,
            "stage": STAGE_ARMED,
            "status": JOB_RUNNING,
            "source": source,
            "reason": reason,
            "triggered_tick": self._clock.tick,
        }
        if successor:
            self._order.remove(successor)
            self._spools[successor]["state"] = SPOOL_RESERVED
            self._publish(
                topics.TOPIC_SCHEDULE_SPOOL_RESERVED,
                {"spool_id": successor, "job_id": job_id, "reason": "turn_up"},
            )
        self._reel.begin_change(reason)
        self._publish(
            topics.TOPIC_SCHEDULE_JOB_TRIGGERED,
            {
                "job_id": job_id,
                "spool_id": spool_id,
                "successor_id": successor,
                "source": source,
                "reason": reason,
                "tick": self._clock.tick,
            },
        )
        return job_id

    def _on_broke_latch(self, event: Any) -> None:
        latched = bool(event.payload)
        if latched:
            if self._active and self._spools[self._active]["state"] == SPOOL_ACTIVE:
                self._spools[self._active]["state"] = SPOOL_INTERRUPTED
            if self._job is not None and self._job["status"] == JOB_RUNNING:
                self._job["status"] = JOB_INTERRUPTED
        else:
            if self._active and self._spools[self._active]["state"] == SPOOL_INTERRUPTED:
                self._spools[self._active]["state"] = SPOOL_ACTIVE
        self._persist()

    def _require_spool(self, spool_id: str) -> dict[str, Any]:
        spool = self._spools.get(spool_id)
        if spool is None:
            raise UnknownKeyError(f"schedule: unknown spool {spool_id}")
        return spool

    def _require_open_job(self, job_id: str) -> dict[str, Any]:
        if self._job is None:
            raise PreGateError("schedule: no open change job")
        if job_id and job_id != self._job["job_id"]:
            raise UnknownKeyError(
                f"schedule: job {job_id} is not open (open job is {self._job['job_id']})"
            )
        return self._job

    def _require_reversible(self, job: dict[str, Any], action: str) -> None:
        if job["status"] not in (JOB_RUNNING, JOB_INTERRUPTED, JOB_DEFERRED):
            raise PreGateError(f"schedule: job {job['job_id']} is already {job['status']}")
        if job["stage"] != STAGE_ARMED:
            raise PreGateError(
                f"schedule: job {job['job_id']} passed {POINT_OF_NO_RETURN}; "
                f"it cannot be {action}ed, only resumed"
            )

    def _require_reserved_successor(self, job: dict[str, Any]) -> str:
        successor = job["successor_id"]
        if not successor or self._spools[successor]["state"] != SPOOL_RESERVED:
            raise PreGateError(f"schedule: job {job['job_id']} has no successor shaft ready")
        return successor

    def _release_successor(self, spool_id: str) -> None:
        self._spools[spool_id]["state"] = SPOOL_WAITING
        self._order.insert(0, spool_id)

    def _publish_step(self, job: dict[str, Any], step: str, extra: dict[str, Any]) -> None:
        payload = {
            "job_id": job["job_id"],
            "spool_id": job["spool_id"],
            "step": step,
            "tick": self._clock.tick,
        }
        payload.update(extra)
        self._publish(topics.TOPIC_SCHEDULE_JOB_STEP, payload)

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        self._bus.publish(topic, payload)

    def _trace_row(self, record: Any) -> dict[str, Any]:
        return {
            "seq": record.seq,
            "record_id": record.record_id,
            "topic": record.topic,
            "tick": record.tick,
            "payload": dict(record.payload),
        }

    def _persist(self) -> None:
        state = {
            "spools": {key: dict(value) for key, value in self._spools.items()},
            "order": list(self._order),
            "active": self._active,
            "job": dict(self._job) if self._job is not None else None,
            "job_counter": self._ids.issued,
            "tick": self._clock.tick,
        }
        with self._journal.transaction("schedule-state") as batch:
            self._journal.append(keys.SCHEDULE_STATE, state, batch)


def _payload_str(payload: Any, name: str) -> str:
    if not isinstance(payload, dict):
        return ""
    value = payload.get(name, "")
    return "" if value is None else str(value)
