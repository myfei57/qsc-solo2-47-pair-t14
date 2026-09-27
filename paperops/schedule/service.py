"""Turn-up scheduling over the reel and broke procedures.

The scheduler owns the queue of empty spools waiting to go on the machine and the
queue of turn-up changes. A diameter reading at or past a spool's target opens a
change; when the machine is free the oldest change takes the oldest waiting
spool and walks the reel procedure (new spool, retract, bite).

A web break holds whatever change is walking: it can be postponed until broke
handling finishes (it then resumes at the same stage) or cancelled while the
old spool is still on the machine (before retract). Every move is appended to
the journal under a spool/change/incident key, so every step traces back to
which spool and which tick.
"""

from typing import Any, Protocol

from ..broke.service import BrokeService
from ..clock import IdSequence, LogicalClock, parse_id
from ..errors import OutOfRangeError, PreGateError, UnknownKeyError
from ..event import Event, EventBus, topics
from ..reel.service import ReelService
from ..store import Journal, keys
from . import calc
from .view import ChangeView, ScheduleView, SpoolView

# Spool lifecycle.
WAITING = "waiting"          # empty spool in the queue
ASSIGNED = "assigned"        # picked for a queued change, not yet on the machine
IN_CHANGE = "in_change"      # change procedure is walking
WINDING = "winding"          # on the machine and taking paper
PENDING_CHANGE = "pending_change"  # full: a change is open but not walking yet
FULL = "full"                # retracted off the machine, done

# Change lifecycle.
QUEUED = "queued"
ACTIVE = "active"
HELD = "held"
CANCELLED = "cancelled"
COMPLETE = "complete"

# Reel procedure stages walked by a change.
STAGE_NEW_SPOOL = "new_spool"
STAGE_RETRACT = "retract"
STAGE_BITE = "bite"

# Scheduling modes.
MODE_AUTO = "auto"          # diameter readings trigger and changes start on their own
MODE_MANUAL = "manual"      # changes still queue, the operator starts them

DEFAULT_TARGET = 1200.0
MAX_RECENT = 20


class CalSource(Protocol):
    """Anything that can report whether the calender nip is loaded."""

    def nip_closed(self) -> bool: ...


class ScheduleService:
    """Queue of waiting spools, queue of changes and break coordination."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        reel: ReelService,
        broke: BrokeService,
        cal: CalSource,
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._reel = reel
        self._broke = broke
        self._cal = cal
        self._spools: dict[str, dict[str, Any]] = {}
        self._changes: dict[str, dict[str, Any]] = {}
        self._queue_order: list[str] = []
        self._incident: dict[str, Any] = {}
        self._mode = MODE_AUTO
        self._change_ids = IdSequence("chg")
        self._incident_ids = IdSequence("brk")

    # ----------------------------------------------------------------- reading

    def mode(self) -> str:
        return self._mode

    def incident(self) -> dict[str, Any]:
        return dict(self._incident)

    def winding_id(self) -> str:
        for spool in self._spools.values():
            if spool["status"] == WINDING:
                return str(spool["spool_id"])
        return ""

    def active_change_id(self) -> str:
        for change in self._changes.values():
            if change["status"] in (ACTIVE, HELD):
                return str(change["change_id"])
        return ""

    def spool(self, spool_id: str) -> dict[str, Any]:
        self._require_spool(spool_id)
        return dict(self._spools[spool_id])

    def change(self, change_id: str) -> dict[str, Any]:
        self._require_change(change_id)
        return dict(self._changes[change_id])

    def waiting_spools(self) -> tuple[dict[str, Any], ...]:
        rows = []
        for position, spool_id in enumerate(self._queue_order):
            spool = self._spools.get(spool_id)
            if spool is not None and spool["status"] in (WAITING, ASSIGNED):
                row = dict(spool)
                row["order"] = position
                rows.append(row)
        return tuple(rows)

    def pending_changes(self) -> tuple[dict[str, Any], ...]:
        rows = [
            dict(change)
            for change in self._changes.values()
            if change["status"] == QUEUED
        ]
        rows.sort(key=lambda change: (int(change["created_tick"]), str(change["change_id"])))
        return tuple(rows)

    def recent_changes(self, limit: int = MAX_RECENT) -> tuple[dict[str, Any], ...]:
        rows = [
            dict(change)
            for change in self._changes.values()
            if change["status"] in (COMPLETE, CANCELLED)
        ]
        rows.sort(key=lambda change: int(change["finished_tick"]), reverse=True)
        return tuple(rows[:limit])

    def view(self) -> ScheduleView:
        active_id = self.active_change_id()
        return ScheduleView(
            mode=self._mode,
            incident=dict(self._incident),
            winding_id=self.winding_id(),
            active_change=self._change_view(active_id).to_dict() if active_id else {},
            queue=tuple(self._spool_views()),
            pending_changes=tuple(self._change_views(self.pending_changes())),
            recent_changes=tuple(self._change_views(self.recent_changes())),
        )

    def timeline(self, spool_id: str) -> list[dict[str, Any]]:
        """Every committed step that names the spool, newest entry last.

        Merges the spool's own rows with every change it took part in, so the
        answer to "what happened to this spool and when" is in one list.
        """
        self._require_spool(spool_id)
        events: list[tuple[int, str, dict[str, Any]]] = []
        for entry in self._journal.history(keys.schedule_spool_key(spool_id)):
            events.append((entry.seq, "spool", entry.value))
        for entry in self._journal.history(keys.schedule_reading_key(spool_id)):
            events.append((entry.seq, "reading", entry.value))
        for change in self._changes.values():
            if spool_id not in (change["outgoing_id"], change["incoming_id"]):
                continue
            for entry in self._journal.history(
                keys.schedule_change_key(str(change["change_id"]))
            ):
                events.append((entry.seq, "change", entry.value))
        events.sort(key=lambda item: item[0])
        return [{"kind": kind, **row} for _, kind, row in events]

    # ------------------------------------------------------------------ queue

    def mount_initial(self, spool_id: str, target: float = DEFAULT_TARGET) -> dict[str, Any]:
        """Register the spool already on the machine at start up."""
        spool_id = self._clean_id(spool_id, "spool")
        if spool_id in self._spools:
            raise PreGateError(f"schedule: spool {spool_id} is already registered")
        if self.winding_id():
            raise PreGateError("schedule: a spool is already winding")
        self._write_spool(
            {
                "spool_id": spool_id,
                "status": WINDING,
                "target_diameter": float(target),
                "diameter": 0.0,
                "enqueued_tick": self._clock.tick,
                "change_id": "",
            }
        )
        return self.spool(spool_id)

    def enqueue(self, spool_id: str, target: float = DEFAULT_TARGET) -> dict[str, Any]:
        """Line an empty spool up behind the ones already waiting."""
        spool_id = self._clean_id(spool_id, "spool")
        if spool_id in self._spools:
            raise PreGateError(f"schedule: spool {spool_id} is already registered")
        self._write_spool(
            {
                "spool_id": spool_id,
                "status": WAITING,
                "target_diameter": float(target),
                "diameter": 0.0,
                "enqueued_tick": self._clock.tick,
                "change_id": "",
            }
        )
        self._queue_order.append(spool_id)
        self._write_queue()
        self._assign_spare_spool(spool_id)
        self._pump()
        return self.spool(spool_id)

    def reorder_queue(self, ordered_ids: list[str]) -> dict[str, Any]:
        """Put the waiting spools in a new order; every waiting spool must appear."""
        waiting = {
            spool_id
            for spool_id in self._queue_order
            if self._spools.get(spool_id, {}).get("status") == WAITING
        }
        if set(ordered_ids) != waiting or len(ordered_ids) != len(waiting):
            raise PreGateError(
                "schedule: the new order must list every waiting spool exactly once"
            )
        head = [
            spool_id
            for spool_id in self._queue_order
            if self._spools.get(spool_id, {}).get("status") == ASSIGNED
        ]
        self._queue_order = head + list(ordered_ids)
        self._write_queue()
        return {"queue": list(self._queue_order)}

    # ----------------------------------------------------------------- change

    def read_diameter(self, spool_id: str, diameter_value: float) -> dict[str, Any]:
        """Report a measured diameter; at target this opens the turn-up change."""
        spool_id = self._clean_id(spool_id, "spool")
        self._require_spool(spool_id)
        if diameter_value < 0:
            raise OutOfRangeError("schedule: diameter must not be negative")
        spool = self._spools[spool_id]
        if spool["status"] not in (WINDING, PENDING_CHANGE, IN_CHANGE):
            raise PreGateError(
                f"schedule: spool {spool_id} is {spool['status']}, not winding"
            )
        target = float(spool["target_diameter"])
        spool["diameter"] = float(diameter_value)
        reading = {
            "spool_id": spool_id,
            "diameter": float(diameter_value),
            "target_diameter": target,
            "tick": self._clock.tick,
        }
        with self._journal.transaction("schedule-reading") as batch:
            self._journal.append(
                keys.schedule_reading_key(spool_id), reading, batch
            )
        self._bus.publish(
            topics.TOPIC_SCHEDULE_SPOOL,
            {
                "spool_id": spool_id,
                "diameter": float(diameter_value),
                "tick": self._clock.tick,
            },
        )
        verdict = calc.change_verdict(float(diameter_value), target)
        triggered = False
        blocked = ""
        if verdict == "turn_up":
            if self._incident:
                blocked = "break_open"
            elif spool["status"] == WINDING and not self._open_change_for(spool_id):
                self._open_change(spool_id, reason="diameter")
                triggered = True
                self._pump()
        return {
            "spool_id": spool_id,
            "diameter": float(diameter_value),
            "target_diameter": target,
            "verdict": verdict,
            "change_opened": triggered,
            "blocked": blocked,
        }

    def request_change(self, spool_id: str, reason: str = "operator") -> dict[str, Any]:
        """Open a turn-up change by hand, without waiting for the diameter."""
        spool_id = self._clean_id(spool_id, "spool")
        self._require_spool(spool_id)
        spool = self._spools[spool_id]
        if spool["status"] not in (WINDING, PENDING_CHANGE):
            raise PreGateError(
                f"schedule: spool {spool_id} is {spool['status']}, it cannot change"
            )
        if self._open_change_for(spool_id):
            raise PreGateError(f"schedule: spool {spool_id} already has an open change")
        change = self._open_change(spool_id, reason=reason)
        self._pump()
        return change

    def start_change(self, change_id: str) -> dict[str, Any]:
        """Take the oldest waiting spool and start a queued change by hand."""
        change_id = self._clean_id(change_id, "change")
        self._require_change(change_id)
        self._start(change_id)
        return self.change(change_id)

    def complete_stage(self) -> dict[str, Any]:
        """Advance the active change through its current reel stage."""
        self._require_no_incident()
        change_id = self.active_change_id()
        if not change_id:
            raise PreGateError("schedule: no change is in progress")
        change = self._changes[change_id]
        if change["status"] != ACTIVE:
            raise PreGateError(f"schedule: change {change_id} is {change['status']}")
        incoming_id = change["incoming_id"]
        stage = change["stage"]
        if stage == STAGE_NEW_SPOOL:
            self._reel.new_spool(incoming_id, self._cal)
            change["stage"] = STAGE_RETRACT
        elif stage == STAGE_RETRACT:
            self._reel.retract_old()
            change["stage"] = STAGE_BITE
        elif stage == STAGE_BITE:
            self._reel.bite()
            self._finish_change(change)
            self._pump()
            return self.change(change_id)
        else:  # pragma: no cover - stage is always one of the three
            raise PreGateError(f"schedule: unknown stage {stage}")
        self._write_change(change)
        return self.change(change_id)

    def cancel_change(self, change_id: str) -> dict[str, Any]:
        """Call a change off; only before the finished spool is retracted."""
        change_id = self._clean_id(change_id, "change")
        self._require_change(change_id)
        change = self._changes[change_id]
        if change["status"] not in (QUEUED, ACTIVE, HELD):
            raise PreGateError(
                f"schedule: change {change_id} is {change['status']} and cannot be cancelled"
            )
        # Retracting is the point of no return: the finished spool leaves the machine.
        if change["status"] != QUEUED and change["stage"] != STAGE_NEW_SPOOL:
            raise PreGateError(
                f"schedule: change {change_id} is retracting the finished spool, "
                "it cannot be cancelled"
            )
        outgoing_id = change["outgoing_id"]
        incoming_id = change["incoming_id"]
        held = change["status"] == HELD

        # The empty spool that had been picked steps back to the queue head.
        if incoming_id:
            spool = self._spools[incoming_id]
            spool["status"] = WAITING
            spool["change_id"] = ""
            self._write_spool(spool)
            if incoming_id in self._queue_order:
                self._queue_order.remove(incoming_id)
            self._queue_order.insert(0, incoming_id)
            self._write_queue()

        change["status"] = CANCELLED
        change["finished_tick"] = self._clock.tick
        self._write_change(change)

        # The spool that still owns the web keeps its turn-up open so it can re-fire.
        outgoing = self._spools[outgoing_id]
        if held:
            # Cancelled mid-break: reopen a queued change that starts after takeover.
            outgoing["change_id"] = ""
            replacement = self._open_change(
                outgoing_id, reason="reschedule_after_break", auto_assign=False
            )
            head = self._next_waiting()
            if head:
                self._assign(replacement, str(head["spool_id"]))
        else:
            outgoing["status"] = WINDING
            outgoing["change_id"] = ""
            self._write_spool(outgoing)

        self._pump()
        return self.change(change_id)

    def set_mode(self, mode: str) -> dict[str, Any]:
        if mode not in (MODE_AUTO, MODE_MANUAL):
            raise OutOfRangeError(f"schedule: unknown mode {mode}")
        self._mode = mode
        with self._journal.transaction("schedule-config") as batch:
            self._journal.append(
                keys.SCHEDULE_CONFIG,
                {"mode": mode, "tick": self._clock.tick},
                batch,
            )
        self._bus.publish(topics.TOPIC_SCHEDULE_CONFIG, {"mode": mode})
        self._pump()
        return {"mode": mode}

    # -------------------------------------------------------------- web break

    def report_break(self, reason: str = "web break") -> dict[str, Any]:
        """Register a web break, trip broke handling and hold the live change."""
        if self._incident:
            raise PreGateError(
                f"schedule: incident {self._incident['incident_id']} is still open"
            )
        incident_id = self._incident_ids.next()
        change_id = self.active_change_id()
        incident = {
            "incident_id": incident_id,
            "reason": reason,
            "status": "open",
            "change_id": change_id,
            "opened_tick": self._clock.tick,
            "closed_tick": 0,
            "resumed_change_id": "",
            "outcome": "",
        }
        with self._journal.transaction("schedule-break") as batch:
            self._journal.append(
                keys.schedule_incident_key(incident_id), incident, batch
            )
        self._bus.publish(
            topics.TOPIC_SCHEDULE_INCIDENT,
            {"incident_id": incident_id, "status": "open", "change_id": change_id},
        )

        # Hand the web break to the broke procedure and latch handling.
        self._broke.trip(reason)

        self._incident = incident
        if change_id:
            change = self._changes[change_id]
            change["status"] = HELD
            change["held_tick"] = self._clock.tick
            self._write_change(change)
        return dict(incident)

    def wire(self) -> None:
        """React to broke handling finishing: resume what the break held."""
        self._bus.subscribe(topics.TOPIC_BROKE_PROCEDURE, self._on_broke_procedure)

    # -------------------------------------------------------------- replay

    def reload(self) -> ScheduleView:
        """Rebuild queues, spools, changes and the open incident from the log."""
        self._spools = {}
        self._changes = {}
        self._incident = {}
        self._mode = MODE_AUTO
        queue_rows: list[Any] = []
        for entry in self._journal.entries():
            if entry.seq > self._journal.watermark:
                continue
            key = entry.key
            if key.startswith(keys.SCHEDULE_SPOOL_PREFIX):
                if key.endswith("/reading"):
                    spool_id = str(entry.value["spool_id"])
                    if spool_id in self._spools:
                        self._spools[spool_id]["diameter"] = float(
                            entry.value["diameter"]
                        )
                    continue
                spool = dict(entry.value)
                self._spools[str(spool["spool_id"])] = spool
                self._restore_id(spool.get("spool_id", ""))
            elif key.startswith(keys.SCHEDULE_CHANGE_PREFIX):
                change = dict(entry.value)
                self._changes[str(change["change_id"])] = change
                self._restore_id(change.get("change_id", ""))
            elif key.startswith(keys.SCHEDULE_INCIDENT_PREFIX):
                incident = dict(entry.value)
                self._restore_id(incident.get("incident_id", ""))
                if incident.get("status") == "open":
                    self._incident = incident
                elif self._incident.get("incident_id") == incident.get("incident_id"):
                    self._incident = {}
            elif key == keys.SCHEDULE_CONFIG:
                self._mode = str(entry.value.get("mode", MODE_AUTO))
            elif key == keys.SCHEDULE_QUEUE:
                queue_rows.append(entry.value)
        self._queue_order = list(queue_rows[-1]) if queue_rows else []
        return self.view()

    # ------------------------------------------------------------- internals

    def _open_change(
        self, outgoing_id: str, reason: str, auto_assign: bool = True
    ) -> dict[str, Any]:
        change_id = self._change_ids.next()
        tick = self._clock.tick
        change = {
            "change_id": change_id,
            "status": QUEUED,
            "outgoing_id": outgoing_id,
            "incoming_id": "",
            "stage": STAGE_NEW_SPOOL,
            "reason": reason,
            "created_tick": tick,
            "started_tick": 0,
            "completed_tick": 0,
            "held_tick": 0,
            "finished_tick": 0,
        }
        self._write_change(change)
        outgoing = self._spools[outgoing_id]
        outgoing["status"] = PENDING_CHANGE
        outgoing["change_id"] = change_id
        self._write_spool(outgoing)
        self._changes[change_id] = change

        # Pair an already-waiting empty spool with the new queued change.
        head_id = self._next_waiting_id() if auto_assign else ""
        if head_id:
            self._assign(change, head_id)
        return dict(change)

    def _assign(self, change: dict[str, Any], spool_id: str) -> None:
        spool = self._spools[spool_id]
        spool["status"] = ASSIGNED
        spool["change_id"] = str(change["change_id"])
        self._write_spool(spool)
        change["incoming_id"] = spool_id
        self._write_change(change)

    def _assign_spare_spool(self, spool_id: str) -> None:
        """A freshly enqueued spool fills the oldest change still missing one."""
        pending = self.pending_changes()
        for change_row in pending:
            if not change_row["incoming_id"]:
                self._assign(self._changes[str(change_row["change_id"])], spool_id)
                return

    def _start(self, change_id: str) -> None:
        self._require_no_incident()
        change = self._changes[change_id]
        if change["status"] != QUEUED:
            raise PreGateError(
                f"schedule: change {change_id} is {change['status']}, not queued"
            )
        incoming_id = change["incoming_id"] or self._next_waiting_id()
        if not incoming_id:
            raise PreGateError(f"schedule: change {change_id} has no waiting spool")
        incoming = self._spools[incoming_id]
        if incoming["status"] == WAITING:
            self._assign(change, incoming_id)
        elif incoming["status"] != ASSIGNED:
            raise PreGateError(
                f"schedule: spool {incoming_id} is {incoming['status']}, not ready"
            )
        if self.active_change_id():
            raise PreGateError("schedule: another change is already in progress")
        outgoing_id = change["outgoing_id"]
        self._reel.begin_change(change["reason"])
        change["status"] = ACTIVE
        change["started_tick"] = self._clock.tick
        self._write_change(change)
        incoming["status"] = IN_CHANGE
        self._write_spool(incoming)
        outgoing = self._spools[outgoing_id]
        outgoing["status"] = IN_CHANGE
        self._write_spool(outgoing)

    def _finish_change(self, change: dict[str, Any]) -> None:
        incoming_id = change["incoming_id"]
        outgoing_id = change["outgoing_id"]
        tick = self._clock.tick
        incoming = self._spools[incoming_id]
        incoming["status"] = WINDING
        incoming["change_id"] = ""
        self._write_spool(incoming)
        outgoing = self._spools[outgoing_id]
        outgoing["status"] = FULL
        outgoing["change_id"] = ""
        self._write_spool(outgoing)
        if outgoing_id in self._queue_order:
            self._queue_order.remove(outgoing_id)
        if incoming_id in self._queue_order:
            self._queue_order.remove(incoming_id)
        self._write_queue()
        change["status"] = COMPLETE
        change["completed_tick"] = tick
        change["finished_tick"] = tick
        self._write_change(change)

    def _pump(self) -> None:
        """In auto mode with the machine free, start the oldest ready change."""
        if self._mode != MODE_AUTO or self._incident:
            return
        if self.active_change_id() or not self._cal.nip_closed():
            return
        for change_row in self.pending_changes():
            change = self._changes[str(change_row["change_id"])]
            if change["incoming_id"] or self._next_waiting_id():
                self._start(str(change["change_id"]))
                return

    def _on_broke_procedure(self, event: Event) -> None:
        """Resume when the broke procedure takes the web back."""
        if not self._incident or event.payload != "complete":
            return
        incident_id = str(self._incident["incident_id"])
        change_id = self._incident.get("change_id", "")
        resumed = ""
        outcome = "web_restored"
        if change_id:
            change = self._changes.get(change_id)
            if change is not None and change["status"] == HELD:
                change["status"] = ACTIVE
                self._write_change(change)
                resumed = change_id
                outcome = "change_resumed"
            else:
                outcome = "change_cancelled"
        incident = dict(self._incident)
        incident["status"] = "closed"
        incident["closed_tick"] = self._clock.tick
        incident["resumed_change_id"] = resumed
        incident["outcome"] = outcome
        self._write_incident(incident_id, incident)
        self._incident = {}
        self._pump()

    def _write_spool(self, spool: dict[str, Any]) -> None:
        spool_id = str(spool["spool_id"])
        with self._journal.transaction("schedule-spool") as batch:
            self._journal.append(keys.schedule_spool_key(spool_id), dict(spool), batch)
        self._spools[spool_id] = dict(spool)
        self._bus.publish(
            topics.TOPIC_SCHEDULE_SPOOL,
            {"spool_id": spool_id, "status": spool["status"]},
        )

    def _write_change(self, change: dict[str, Any]) -> None:
        change_id = str(change["change_id"])
        with self._journal.transaction("schedule-change") as batch:
            self._journal.append(
                keys.schedule_change_key(change_id), dict(change), batch
            )
        self._changes[change_id] = dict(change)
        self._bus.publish(
            topics.TOPIC_SCHEDULE_CHANGE,
            {
                "change_id": change_id,
                "status": change["status"],
                "stage": change["stage"],
                "outgoing_id": change["outgoing_id"],
                "incoming_id": change["incoming_id"],
            },
        )

    def _write_incident(self, incident_id: str, incident: dict[str, Any]) -> None:
        with self._journal.transaction("schedule-incident") as batch:
            self._journal.append(
                keys.schedule_incident_key(incident_id), dict(incident), batch
            )
        self._bus.publish(
            topics.TOPIC_SCHEDULE_INCIDENT,
            {
                "incident_id": incident_id,
                "status": incident["status"],
                "change_id": incident.get("change_id", ""),
                "outcome": incident.get("outcome", ""),
            },
        )

    def _write_queue(self) -> None:
        with self._journal.transaction("schedule-queue") as batch:
            self._journal.append(
                keys.SCHEDULE_QUEUE, list(self._queue_order), batch
            )
        self._bus.publish(
            topics.TOPIC_SCHEDULE_QUEUE, {"queue": list(self._queue_order)}
        )

    def _next_waiting_id(self) -> str:
        for spool_id in self._queue_order:
            spool = self._spools.get(spool_id)
            if spool is not None and spool["status"] == WAITING:
                return spool_id
        return ""

    def _next_waiting(self) -> dict[str, Any] | None:
        spool_id = self._next_waiting_id()
        return dict(self._spools[spool_id]) if spool_id else None

    def _open_change_for(self, spool_id: str) -> dict[str, Any] | None:
        for change in self._changes.values():
            if (
                change["outgoing_id"] == spool_id
                and change["status"] in (QUEUED, ACTIVE, HELD)
            ):
                return change
        return None

    def _spool_views(self) -> list[SpoolView]:
        views = []
        for position, spool_id in enumerate(self._queue_order):
            spool = self._spools.get(spool_id)
            if spool is None or spool["status"] not in (WAITING, ASSIGNED):
                continue
            views.append(
                SpoolView(
                    spool_id=str(spool["spool_id"]),
                    status=str(spool["status"]),
                    target_diameter=float(spool["target_diameter"]),
                    diameter=float(spool["diameter"]),
                    enqueued_tick=int(spool["enqueued_tick"]),
                    order=position,
                    change_id=str(spool.get("change_id", "")),
                )
            )
        return views

    def _change_views(self, rows: Any) -> list[ChangeView]:
        return [self._change_view(str(row["change_id"])) for row in rows]

    def _change_view(self, change_id: str) -> ChangeView:
        change = self._changes[change_id]
        history = tuple(
            dict(entry.value)
            for entry in self._journal.history(keys.schedule_change_key(change_id))
        )
        return ChangeView(
            change_id=str(change["change_id"]),
            status=str(change["status"]),
            outgoing_id=str(change["outgoing_id"]),
            incoming_id=str(change["incoming_id"]),
            stage=str(change["stage"]),
            reason=str(change["reason"]),
            created_tick=int(change["created_tick"]),
            started_tick=int(change["started_tick"]),
            completed_tick=int(change["completed_tick"]),
            held_tick=int(change["held_tick"]),
            finished_tick=int(change["finished_tick"]),
            history=history,
        )

    def _require_spool(self, spool_id: str) -> None:
        if spool_id not in self._spools:
            raise UnknownKeyError(f"schedule: unknown spool {spool_id}")

    def _require_change(self, change_id: str) -> None:
        if change_id not in self._changes:
            raise UnknownKeyError(f"schedule: unknown change {change_id}")

    def _require_no_incident(self) -> None:
        if self._incident:
            raise PreGateError(
                f"schedule: incident {self._incident['incident_id']} is open; "
                "finish broke handling first"
            )

    @staticmethod
    def _clean_id(value: str, kind: str) -> str:
        text = str(value).strip()
        if not text:
            raise PreGateError(f"schedule: {kind} id must not be empty")
        return text

    def _restore_id(self, identifier: str) -> None:
        number = parse_id(identifier)
        if number is None:
            return
        prefix = identifier[: identifier.rfind("-")]
        if prefix == "chg":
            self._change_ids.restore(number)
        elif prefix == "brk":
            self._incident_ids.restore(number)
