"""Read model for turn-up scheduling."""

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class SpoolView:
    """One reel spool and its place on the machine."""

    spool_id: str
    status: str
    target_diameter: float
    diameter: float
    enqueued_tick: int
    order: int
    change_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "spool_id": self.spool_id,
            "status": self.status,
            "target_diameter": self.target_diameter,
            "diameter": self.diameter,
            "enqueued_tick": self.enqueued_tick,
            "order": self.order,
            "change_id": self.change_id,
        }


@dataclass(frozen=True)
class ChangeView:
    """One scheduled turn-up and the procedure stage it is sitting at."""

    change_id: str
    status: str
    outgoing_id: str
    incoming_id: str
    stage: str
    reason: str
    created_tick: int
    started_tick: int
    completed_tick: int
    held_tick: int
    finished_tick: int
    history: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "status": self.status,
            "outgoing_id": self.outgoing_id,
            "incoming_id": self.incoming_id,
            "stage": self.stage,
            "reason": self.reason,
            "created_tick": self.created_tick,
            "started_tick": self.started_tick,
            "completed_tick": self.completed_tick,
            "held_tick": self.held_tick,
            "finished_tick": self.finished_tick,
            "history": [dict(row) for row in self.history],
        }


@dataclass(frozen=True)
class ScheduleView:
    """Everything the operator needs to see about waiting spools and changes."""

    mode: str
    incident: Mapping[str, Any]
    winding_id: str
    active_change: Mapping[str, Any]
    queue: tuple[SpoolView, ...]
    pending_changes: tuple[ChangeView, ...]
    recent_changes: tuple[ChangeView, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "incident": dict(self.incident),
            "winding_id": self.winding_id,
            "active_change": dict(self.active_change),
            "queue": [spool.to_dict() for spool in self.queue],
            "pending_changes": [change.to_dict() for change in self.pending_changes],
            "recent_changes": [change.to_dict() for change in self.recent_changes],
        }
