"""Read model for reel change scheduling."""

from dataclasses import dataclass
from typing import Any, Mapping

# Lifecycle of a single spool (one winding shaft/core).
SPOOL_WAITING = "waiting"
SPOOL_RESERVED = "reserved"
SPOOL_ACTIVE = "active"
SPOOL_INTERRUPTED = "interrupted"
SPOOL_DOFFED = "doffed"

# Lifecycle of a reel change job.
JOB_RUNNING = "running"
JOB_INTERRUPTED = "interrupted"
JOB_DEFERRED = "deferred"
JOB_DONE = "done"
JOB_CANCELLED = "cancelled"

# The stage of a change job after which it can no longer be cancelled or deferred:
# once the new spool is turned up the web is already being cut over.
POINT_OF_NO_RETURN = "new_spool"


@dataclass(frozen=True)
class ScheduleView:
    """Every spool, the open change job (if any) and the broke latch position."""

    spools: tuple[Mapping[str, Any], ...]
    queue: tuple[str, ...]
    active: str
    job: Mapping[str, Any] | None
    broke_latched: bool
    tick: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "spools": [dict(spool) for spool in self.spools],
            "queue": list(self.queue),
            "active": self.active,
            "job": dict(self.job) if self.job is not None else None,
            "broke_latched": self.broke_latched,
            "tick": self.tick,
        }
