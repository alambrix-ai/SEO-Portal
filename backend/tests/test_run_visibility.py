"""What the console shows about a run must match what the run recorded."""
from __future__ import annotations

from datetime import timedelta

from app.core.config import settings
from app.db.base import utcnow
from app.models.agent import AgentRun, close_timeline, record_timeline_step
from app.services import views


def _run(**kw) -> AgentRun:
    values = dict(
        id="r1",
        agent_slug="on_page_seo_sync",
        trigger="manual",
        triggered_by="Priya",
        status="running",
        started_at=utcnow() - timedelta(seconds=30),
        finished_at=None,
        duration_ms=0,
        actions_taken=0,
        actions_queued=0,
        summary="",
        error="",
        cost=0.0,
        step="",
        step_done=0,
        step_total=0,
        detail={},
    )
    values.update(kw)
    return AgentRun(**values)


def test_a_counter_moving_updates_the_step_instead_of_adding_one():
    detail = record_timeline_step({}, "Checking its connectors and scope")
    detail = record_timeline_step(detail, "Applying what it found (1 of 3)", done=1, total=3)
    detail = record_timeline_step(detail, "Applying what it found (2 of 3)", done=2, total=3)
    labels = [entry["label"] for entry in detail["timeline"]]
    assert labels == ["Checking its connectors and scope", "Applying what it found"]
    assert detail["timeline"][-1]["done"] == 2
    assert detail["timeline"][0]["ended_at"]  # closed when the next step began


def test_other_detail_keys_survive_and_the_timeline_is_bounded():
    detail = {"pages": 4}
    for index in range(100):
        detail = record_timeline_step(detail, f"Analysing /page-{index}")
    assert detail["pages"] == 4
    assert len(detail["timeline"]) == 40
    assert detail["timeline"][-1]["label"] == "Analysing /page-99"


def test_finishing_closes_the_last_step():
    detail = close_timeline(record_timeline_step({}, "Writing"))
    assert detail["timeline"][-1]["ended_at"]
    assert close_timeline({}) == {}


def test_a_run_past_the_lease_is_reported_as_interrupted_not_working():
    stale = _run(started_at=utcnow() - timedelta(minutes=settings.scheduler_claim_lease_minutes + 1))
    assert views.run_state(stale) == "interrupted"
    assert views.run_state(_run()) == "running"
    progress = views._run_progress(stale)
    assert progress["busy"] is False
    assert "interrupted" in progress["last_run_summary"]


def test_run_out_carries_the_timeline_and_live_percent():
    detail = record_timeline_step({}, "Analysing pages (3 of 6)", done=3, total=6)
    out = views.run_out(_run(detail=detail, step="Analysing pages (3 of 6)", step_done=3, step_total=6))
    assert out.state == "running"
    assert out.step_percent == 50
    assert out.timeline[0].label == "Analysing pages"
    assert out.timeline[0].percent == 50
    assert out.last_update_at is not None


def test_a_finished_run_reports_its_duration_and_no_live_step():
    out = views.run_out(
        _run(status="succeeded", duration_ms=134_000, finished_at=utcnow(), step="stale step")
    )
    assert out.state_label == "Completed"
    assert out.duration_label == "2m 14s"
    assert out.step == ""
