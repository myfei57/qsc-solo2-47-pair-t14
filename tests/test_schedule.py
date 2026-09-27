"""Automatic reel change scheduling, break recovery and shaft traceability."""

from support import build, ok, refused


def prepare_nip(console) -> None:
    ok(console, "POST", "/api/press/dewater", {"inlet_moisture": 60, "width": 6})
    ok(console, "POST", "/api/press/persist", {"speed": 900})
    ok(console, "POST", "/api/dry/speed", {"speed": 900})
    ok(console, "POST", "/api/cal/speed", {"speed": 900, "target": 900})
    ok(console, "POST", "/api/cal/nip", {"closed": True, "pressure": 60})


def enlist_line(console, *spool_ids, target=1000):
    for spool_id in spool_ids:
        ok(console, "POST", "/api/schedule/enlist", {"spool_id": spool_id, "target": target})


def finish_change(console):
    ok(console, "POST", "/api/schedule/advance", {})
    ok(console, "POST", "/api/schedule/advance", {})
    ok(console, "POST", "/api/schedule/advance", {})


def state(console):
    return ok(console, "GET", "/api/schedule/state")


def test_enlist_queue_and_mount(console):
    enlist_line(console, "S-1", "S-2", "S-3")
    body = state(console)
    assert body["queue"] == ["S-1", "S-2", "S-3"]
    assert body["active"] == ""
    body = ok(console, "POST", "/api/schedule/mount", {})
    assert body["state"] == "active"
    assert state(console)["queue"] == ["S-2", "S-3"]


def test_reorder_moves_a_waiting_spool(console):
    enlist_line(console, "S-1", "S-2", "S-3")
    body = ok(console, "POST", "/api/schedule/reorder", {"spool_id": "S-3", "position": 0})
    assert body["state"] == "waiting"
    assert state(console)["queue"] == ["S-3", "S-1", "S-2"]


def test_diameter_triggers_a_change_and_reserves_the_head(console, services):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    services.clock.advance(1)
    body = ok(console, "POST", "/api/schedule/measure", {"spool_id": "S-1", "diameter": 500})
    assert body["triggered"] is False
    services.clock.advance(2)
    body = ok(console, "POST", "/api/schedule/measure", {"spool_id": "S-1", "diameter": 1000})
    assert body["triggered"] is True
    assert body["growth"] == 250.0
    assert body["eta"] == 0.0
    body = state(console)
    assert body["job"]["stage"] == "armed"
    assert body["job"]["successor_id"] == "S-2"
    assert body["queue"] == []
    assert {spool["spool_id"]: spool["state"] for spool in body["spools"]} == {
        "S-1": "active",
        "S-2": "reserved",
    }


def test_full_change_walks_three_stages(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2", "S-3")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {"reason": "manual turn up"})
    finish_change(console)
    body = state(console)
    assert body["active"] == "S-2"
    assert body["job"] is None
    assert body["queue"] == ["S-3"]
    states = {spool["spool_id"]: spool["state"] for spool in body["spools"]}
    assert states["S-1"] == "doffed"
    assert states["S-2"] == "active"


def test_armed_change_can_be_cancelled_before_the_cut(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2", "S-3")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    body = ok(console, "POST", "/api/schedule/cancel", {})
    assert body["status"] == "cancelled"
    body = state(console)
    assert body["job"] is None
    assert body["queue"][0] == "S-2"
    assert body["active"] == "S-1"
    assert console.services.reel.procedure()["next"] == "new_spool"


def test_break_interrupts_an_armed_job_and_resume_finishes_it(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    ok(console, "POST", "/api/broke/trip", {"reason": "snap"})
    body = state(console)
    assert body["broke_latched"] is True
    assert body["job"]["status"] == "interrupted"
    refused(console, "POST", "/api/schedule/advance", {})
    # resume is refused while the broke is still being handled
    refused(console, "POST", "/api/schedule/resume", {})
    ok(console, "POST", "/api/broke/process", {"volume": 12, "batch_id": "B-1"})
    ok(console, "POST", "/api/broke/reset", {})
    ok(console, "POST", "/api/broke/takeover", {})
    ok(console, "POST", "/api/schedule/resume", {})
    finish_change(console)
    body = state(console)
    assert body["active"] == "S-2"
    assert body["job"] is None


def test_deferred_job_is_resumed_after_broke_handling(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    ok(console, "POST", "/api/broke/trip", {"reason": "snap"})
    body = ok(console, "POST", "/api/schedule/defer", {})
    assert body["status"] == "deferred"
    body = state(console)
    assert body["queue"][0] == "S-2"
    ok(console, "POST", "/api/broke/process", {"volume": 5, "batch_id": "B-1"})
    ok(console, "POST", "/api/broke/reset", {})
    ok(console, "POST", "/api/broke/takeover", {})
    ok(console, "POST", "/api/schedule/resume", {})
    finish_change(console)
    assert state(console)["active"] == "S-2"


def test_change_after_the_cut_cannot_be_cancelled(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    ok(console, "POST", "/api/schedule/advance", {})  # new spool turned up
    ok(console, "POST", "/api/broke/trip", {"reason": "snap"})
    body = state(console)
    assert body["job"]["stage"] == "retract"
    response = refused(console, "POST", "/api/schedule/cancel", {})
    assert response.status == 409
    assert "new_spool" in response.body["error"]
    refused(console, "POST", "/api/schedule/defer", {})
    ok(console, "POST", "/api/broke/process", {"volume": 5, "batch_id": "B-1"})
    ok(console, "POST", "/api/broke/reset", {})
    ok(console, "POST", "/api/broke/takeover", {})
    ok(console, "POST", "/api/schedule/resume", {})
    ok(console, "POST", "/api/schedule/advance", {})
    ok(console, "POST", "/api/schedule/advance", {})
    assert state(console)["active"] == "S-2"


def test_measure_is_blocked_while_the_broke_latch_is_set(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/broke/trip", {"reason": "snap"})
    response = refused(console, "POST", "/api/schedule/measure", {"spool_id": "S-1", "diameter": 1000})
    assert response.status == 409


def test_eta_is_derived_from_diameter_growth(console, services):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    services.clock.advance(1)
    ok(console, "POST", "/api/schedule/measure", {"spool_id": "S-1", "diameter": 800})
    services.clock.advance(2)
    body = ok(console, "POST", "/api/schedule/measure", {"spool_id": "S-1", "diameter": 900})
    # 100 mm over 2 ticks -> 50/tick, 100 mm left -> 2 ticks
    assert body["growth"] == 50.0
    assert body["eta"] == 2.0
    assert body["triggered"] is False


def test_spool_and_job_traces_cover_every_move(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    job_id = state(console)["job"]["job_id"]
    finish_change(console)
    trace = ok(console, "GET", f"/api/schedule/trace/spool?spool_id=S-1")["events"]
    topics = [event["topic"] for event in trace]
    assert topics[0] == "schedule.spool.enlisted"
    assert "schedule.spool.doffed" in topics
    assert all(event["tick"] >= 0 for event in trace)
    job_trace = ok(console, "GET", f"/api/schedule/trace/job?job_id={job_id}")["events"]
    steps = [event["payload"]["step"] for event in job_trace if event["topic"] == "schedule.job.step"]
    assert steps == ["new_spool", "retract", "bite"]
    # diameter readings belong to the winding shaft, never to the reserved successor
    successor_trace = ok(console, "GET", "/api/schedule/trace/spool?spool_id=S-2")["events"]
    assert "schedule.diameter.measured" not in [event["topic"] for event in successor_trace]


def test_schedule_survives_a_restart_mid_change(config):
    first = build(config)
    prepare_nip(first.console)
    enlist_line(first.console, "S-1", "S-2", "S-3")
    ok(first.console, "POST", "/api/schedule/mount", {})
    ok(first.console, "POST", "/api/schedule/trigger", {})
    ok(first.console, "POST", "/api/schedule/advance", {})
    ok(first.console, "POST", "/api/broke/trip", {"reason": "snap"})

    second = build(config)
    body = state(second.console)
    assert body["job"]["status"] == "interrupted"
    assert body["job"]["stage"] == "retract"
    assert body["active"] == "S-1"
    assert {spool["spool_id"]: spool["state"] for spool in body["spools"]}["S-2"] == "reserved"
    ok(second.console, "POST", "/api/broke/process", {"volume": 9, "batch_id": "B-1"})
    ok(second.console, "POST", "/api/broke/reset", {})
    ok(second.console, "POST", "/api/broke/takeover", {})
    ok(second.console, "POST", "/api/schedule/resume", {})
    ok(second.console, "POST", "/api/schedule/advance", {})
    ok(second.console, "POST", "/api/schedule/advance", {})
    body = state(second.console)
    assert body["active"] == "S-2"
    assert body["job"] is None

    # the job id counter stays ahead of every id recovered from the log
    second.clock.advance(1)
    ok(second.console, "POST", "/api/schedule/measure", {"spool_id": "S-2", "diameter": 1000})
    assert state(second.console)["job"]["job_id"] == "job-000002"


def test_second_trigger_is_refused_while_one_job_is_open(console):
    prepare_nip(console)
    enlist_line(console, "S-1", "S-2")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    response = refused(console, "POST", "/api/schedule/trigger", {})
    assert response.status == 409


def test_change_runs_without_a_successor_but_cannot_cut_over(console):
    prepare_nip(console)
    enlist_line(console, "S-1")
    ok(console, "POST", "/api/schedule/mount", {})
    ok(console, "POST", "/api/schedule/trigger", {})
    body = state(console)
    assert body["job"]["successor_id"] == ""
    response = refused(console, "POST", "/api/schedule/advance", {})
    assert response.status == 409
