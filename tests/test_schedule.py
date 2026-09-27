"""Turn-up scheduling: diameter trigger, spool queue and break coordination."""

from support import build, ok, refused


def prepare_line(console):
    """Bring the line to running with one spool on the machine and spools queued."""
    ok(console, "POST", "/api/clock/advance", {"steps": 1})
    ok(console, "POST", "/api/press/dewater", {"inlet_moisture": 60, "width": 6})
    ok(console, "POST", "/api/press/persist", {"speed": 900})
    ok(console, "POST", "/api/dry/speed", {"speed": 900})
    ok(console, "POST", "/api/cal/speed", {"speed": 900, "target": 900})
    ok(console, "POST", "/api/cal/nip", {"closed": True, "pressure": 60})


def mount_and_queue(console, mounted="S-1", waiting=("S-2", "S-3")):
    ok(console, "POST", "/api/schedule/mount", {"spool_id": mounted, "target_diameter": 1200})
    for spool_id in waiting:
        ok(console, "POST", "/api/schedule/enqueue", {"spool_id": spool_id, "target_diameter": 1200})


def test_diameter_opens_and_starts_change_automatically(console, services):
    prepare_line(console)
    mount_and_queue(console)
    body = ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    assert body["verdict"] == "turn_up"
    assert body["change_opened"] is True
    state = services.schedule.view().to_dict()
    assert state["active_change"]["outgoing_id"] == "S-1"
    assert state["active_change"]["incoming_id"] == "S-2"
    assert state["active_change"]["stage"] == "new_spool"
    assert services.reel.procedure()["next"] == "new_spool"
    assert state["queue"][0]["spool_id"] == "S-3"


def test_below_target_keeps_winding(console, services):
    prepare_line(console)
    mount_and_queue(console)
    body = ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 900})
    assert body["verdict"] == "winding"
    assert body["change_opened"] is False
    assert services.schedule.active_change_id() == ""


def test_without_a_waiting_spool_the_change_queues(console, services):
    prepare_line(console)
    ok(console, "POST", "/api/schedule/mount", {"spool_id": "S-1"})
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    assert services.schedule.active_change_id() == ""
    pending = services.schedule.pending_changes()
    assert len(pending) == 1
    assert pending[0]["incoming_id"] == ""
    # A spool arriving later joins the change but does not jump FIFO for older ones.
    ok(console, "POST", "/api/schedule/enqueue", {"spool_id": "S-2"})
    assert services.schedule.change(pending[0]["change_id"])["incoming_id"] == "S-2"
    assert services.schedule.active_change_id() == pending[0]["change_id"]


def test_change_walks_every_stage_and_hands_over(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    change_id = services.schedule.active_change_id()
    ok(console, "POST", "/api/schedule/step", {})
    assert services.schedule.change(change_id)["stage"] == "retract"
    assert services.reel.spool_id() == "S-2"
    ok(console, "POST", "/api/schedule/step", {})
    assert services.schedule.change(change_id)["stage"] == "bite"
    ok(console, "POST", "/api/schedule/step", {})
    change = services.schedule.change(change_id)
    assert change["status"] == "complete"
    assert services.schedule.winding_id() == "S-2"
    assert services.schedule.spool("S-1")["status"] == "full"
    recent = ok(console, "GET", "/api/schedule/state")["recent_changes"]
    assert recent[0]["change_id"] == change_id


def test_break_holds_change_and_takeover_resumes_it(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    change_id = services.schedule.active_change_id()
    ok(console, "POST", "/api/schedule/step", {})  # new spool S-2 loaded, web still on S-1
    # Web breaks: the change is postponed onto the broke procedure.
    incident = ok(console, "POST", "/api/schedule/break", {"reason": "snap"})
    assert services.broke.latched() is True
    assert services.schedule.change(change_id)["status"] == "held"
    # Procedure steps are refused while the incident is open.
    response = refused(console, "POST", "/api/schedule/step", {})
    assert response.status == 409
    # Diameter readings during the break cannot open a second change.
    blocked = ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1250})
    assert blocked["blocked"] == "break_open"
    # Broke handled, latch cleared, web taken back: the change resumes where it stopped.
    ok(console, "POST", "/api/broke/process", {"volume": 12, "batch_id": "B-1"})
    ok(console, "POST", "/api/broke/reset", {})
    ok(console, "POST", "/api/broke/takeover", {})
    change = services.schedule.change(change_id)
    assert change["status"] == "active"
    assert change["stage"] == "retract"
    closed = services.schedule.incident()
    assert closed == {}
    ok(console, "POST", "/api/schedule/step", {})
    ok(console, "POST", "/api/schedule/step", {})
    assert services.schedule.change(change_id)["status"] == "complete"
    assert incident["incident_id"].startswith("brk-")


def test_break_without_a_change_trips_broke_and_resumes_winding(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/schedule/break", {"reason": "edge tear"})
    assert services.broke.latched() is True
    assert services.schedule.active_change_id() == ""
    ok(console, "POST", "/api/broke/process", {"volume": 5, "batch_id": "B-1"})
    ok(console, "POST", "/api/broke/reset", {})
    ok(console, "POST", "/api/broke/takeover", {})
    assert services.schedule.incident() == {}
    body = ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    assert body["change_opened"] is True


def test_change_cancelled_during_new_spool_refires_after_break(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    change_id = services.schedule.active_change_id()
    # Break lands while the change is still on the new-spool stage, before retract.
    ok(console, "POST", "/api/schedule/break", {"reason": "snap"})
    assert services.schedule.change(change_id)["stage"] == "new_spool"
    cancelled = ok(console, "POST", "/api/schedule/cancel", {"change_id": change_id})
    assert cancelled["status"] == "cancelled"
    # The empty spool goes back to the head of the queue.
    assert services.schedule.waiting_spools()[0]["spool_id"] == "S-2"
    # A replacement change is queued and starts itself once broke handling finishes.
    ok(console, "POST", "/api/broke/process", {"volume": 10, "batch_id": "B-1"})
    ok(console, "POST", "/api/broke/reset", {})
    ok(console, "POST", "/api/broke/takeover", {})
    new_id = services.schedule.active_change_id()
    assert new_id and new_id != change_id
    change = services.schedule.change(new_id)
    assert change["outgoing_id"] == "S-1"
    assert change["incoming_id"] == "S-2"


def test_cancel_is_refused_after_the_finished_spool_is_retracted(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    change_id = services.schedule.active_change_id()
    ok(console, "POST", "/api/schedule/step", {})
    ok(console, "POST", "/api/schedule/step", {})  # retract closed
    response = refused(console, "POST", "/api/schedule/cancel", {"change_id": change_id})
    assert response.status == 409


def test_manual_mode_waits_for_the_operator(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/schedule/mode", {"mode": "manual"})
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    assert services.schedule.active_change_id() == ""
    pending = services.schedule.pending_changes()
    assert len(pending) == 1
    ok(console, "POST", "/api/schedule/start", {"change_id": pending[0]["change_id"]})
    assert services.schedule.active_change_id() == pending[0]["change_id"]


def test_fifo_order_is_kept_and_can_be_reshaped(console, services):
    prepare_line(console)
    ok(console, "POST", "/api/schedule/mount", {"spool_id": "S-1"})
    for spool_id in ("S-2", "S-3", "S-4"):
        ok(console, "POST", "/api/schedule/enqueue", {"spool_id": spool_id})
    ok(console, "POST", "/api/schedule/reorder", {"ordered_ids": ["S-4", "S-2", "S-3"]})
    order = [spool["spool_id"] for spool in services.schedule.waiting_spools()]
    assert order == ["S-4", "S-2", "S-3"]
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    assert services.schedule.active_change_id()
    assert services.schedule.view().to_dict()["active_change"]["incoming_id"] == "S-4"


def test_every_step_traces_to_a_spool_and_a_tick(console, services):
    prepare_line(console)
    mount_and_queue(console)
    ok(console, "POST", "/api/clock/advance", {"steps": 4})
    ok(console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    ok(console, "POST", "/api/schedule/step", {})
    ok(console, "POST", "/api/schedule/step", {})
    ok(console, "POST", "/api/schedule/step", {})
    timeline = ok(console, "GET", "/api/schedule/timeline?spool_id=S-1")["events"]
    statuses = [event["status"] for event in timeline if event["kind"] == "spool"]
    assert statuses == [
        "winding",
        "pending_change",
        "in_change",
        "full",
    ]
    assert all(
        ("tick" in event) or ("enqueued_tick" in event) or ("created_tick" in event)
        for event in timeline
    )
    change_events = [event for event in timeline if event["kind"] == "change"]
    assert change_events[-1]["status"] == "complete"
    # The audit log carries the published schedule events too.
    audit = ok(console, "GET", "/api/audit?topic=schedule.spool.changed")["records"]
    assert any(record["payload"]["spool_id"] == "S-2" for record in audit)


def test_schedule_survives_a_restart(config):
    first = build(config)
    prepare_line(first.console)
    mount_and_queue(first.console)
    ok(first.console, "POST", "/api/schedule/diameter", {"spool_id": "S-1", "diameter": 1200})
    change_id = first.schedule.active_change_id()
    ok(first.console, "POST", "/api/schedule/step", {})
    ok(first.console, "POST", "/api/schedule/break", {"reason": "snap"})

    second = build(config)
    assert second.schedule.change(change_id)["status"] == "held"
    assert second.schedule.incident()["change_id"] == change_id
    assert second.schedule.waiting_spools()[0]["spool_id"] == "S-3"
    ok(second.console, "POST", "/api/broke/process", {"volume": 12, "batch_id": "B-1"})
    ok(second.console, "POST", "/api/broke/reset", {})
    ok(second.console, "POST", "/api/broke/takeover", {})
    assert second.schedule.change(change_id)["status"] == "active"
    ok(second.console, "POST", "/api/schedule/step", {})
    ok(second.console, "POST", "/api/schedule/step", {})
    assert second.schedule.winding_id() == "S-2"
