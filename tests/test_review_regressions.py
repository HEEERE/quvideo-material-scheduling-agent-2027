"""Regressions found during review; controlled models are not live-agent evidence."""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest
from pydantic import ValidationError

from conftest import call, prepare
from test_agent import ScriptedModel, action
from test_guard import conflict_request
from src.agent import AgentRuntime
from src.main import main
from src.models import Material, ScheduleRequest
from src.rules import MaterialService, ReviewService
from src.storage import DraftRepository
from src.tracing import TraceRecorder

OLD = "2027-01-15T19:00:00-05:00"
NEW = "2027-01-15T21:00:00-05:00"


def occupy(repository, target=OLD):
    with closing(repository.connect()) as connection:
        connection.execute("INSERT INTO formal_schedules VALUES(?,?,?,?,?)",
                           ("REVIEW-NEW-CONFLICT", "TikTok", "US", target, "CONFIRMED"))
        connection.commit()


def save_pending(registry, target=OLD):
    prepare(registry, time=target)
    return call(registry, "save_schedule_draft", suggested_publish_time=target, confirmation_status="PENDING")


def test_stale_pending_replans_same_id_with_history(request_data, registry_factory):
    request = conflict_request(request_data)
    first_registry = registry_factory(request)
    first = save_pending(first_registry)
    occupy(first_registry.repository)
    repeated = registry_factory(request)
    result = save_pending(repeated, NEW)
    assert result["status"] == "SAVED"
    assert result["action"] == "REPLANNED"
    assert result["draft_id"] == first["draft_id"]
    assert result["draft"]["suggested_publish_time"] == NEW
    assert result["draft"]["confirmation_status"] == "PENDING"
    assert result["draft"]["draft_status"] == "AWAITING_CONFIRMATION"
    assert result["draft"]["original_publish_time"] == first["draft"]["original_publish_time"]
    assert result["draft"]["created_at"] == first["draft"]["created_at"]
    assert result["draft"]["revision"] == 2
    assert len(repeated.repository.all()) == 1
    history = repeated.repository.history(first["draft_id"])
    assert len(history) == 1
    assert history[0]["draft"]["suggested_publish_time"] == OLD
    assert repeated.outcomes()[0]["schedule_check"]["available"] is True


def test_valid_pending_repeat_does_not_rewrite(request_data, registry_factory):
    request = conflict_request(request_data)
    first = save_pending(registry_factory(request))
    repeated = registry_factory(request)
    result = save_pending(repeated, NEW)
    assert result["status"] == "ALREADY_EXISTS"
    assert result["draft_id"] == first["draft_id"]
    assert result["draft"]["suggested_publish_time"] == OLD
    assert repeated.repository.history(first["draft_id"]) == []


@pytest.mark.parametrize("target,confirmation", [(NEW, "WITHIN_FLEXIBILITY"),
                                                   (NEW, "USER_CONFIRMED"),
                                                   ("2027-01-15T22:00:00-05:00", "PENDING")])
def test_replan_keeps_confirmation_and_window_guard(request_data, registry_factory, target, confirmation):
    request = conflict_request(request_data)
    registry = registry_factory(request)
    first = save_pending(registry)
    occupy(registry.repository)
    repeated = registry_factory(request)
    prepare(repeated, time=target)
    result = call(repeated, "save_schedule_draft", suggested_publish_time=target, confirmation_status=confirmation)
    assert result["status"] == "REJECTED_BY_POLICY"
    assert repeated.repository.get(request.fingerprint(), "M01")["suggested_publish_time"] == OLD
    assert repeated.repository.history(first["draft_id"]) == []
    assert repeated.outcomes()[0]["status"] != "DRAFT_SAVED"


def test_replan_failure_rolls_back_update_and_history(request_data, registry_factory):
    request = conflict_request(request_data)
    registry = registry_factory(request)
    first = save_pending(registry)
    occupy(registry.repository)
    repeated = registry_factory(request, failure=True)
    result = save_pending(repeated, NEW)
    assert result["status"] == "SAVE_FAILED"
    assert repeated.repository.get(request.fingerprint(), "M01")["suggested_publish_time"] == OLD
    assert repeated.repository.history(first["draft_id"]) == []
    assert len(repeated.repository.all()) == 1


def test_concurrent_replan_only_revises_once(request_data, registry_factory):
    request = conflict_request(request_data)
    registry = registry_factory(request)
    first = save_pending(registry)
    occupy(registry.repository)
    registries = [registry_factory(request) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save_pending_at_new, registries))
    assert sum(r["status"] == "SAVED" for r in results) == 1
    assert len({r["draft_id"] for r in results}) == 1
    assert len(registry.repository.history(first["draft_id"])) == 1


def save_pending_at_new(registry):
    return save_pending(registry, NEW)


def test_ready_conflict_is_not_silently_reapproved(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    prepare(registry)
    first = call(registry, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    occupy(registry.repository, schedule_request.requested_publish_time.isoformat())
    repeated = registry_factory(schedule_request)
    prepare(repeated)
    result = call(repeated, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    assert result["status"] == "REJECTED_BY_POLICY"
    assert "EXISTING_DRAFT_INVALID" in result["error"]
    assert len(repeated.repository.all()) == 1
    assert repeated.repository.history(first["draft_id"]) == []
    assert repeated.outcomes()[0]["status"] == "NEEDS_CONFIRMATION"


def test_conflict_after_commit_is_reported_in_final_outcome(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    prepare(registry)
    call(registry, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    occupy(registry.repository, schedule_request.requested_publish_time.isoformat())
    outcome = registry.outcomes()[0]
    assert outcome["status"] == "NEEDS_CONFIRMATION"
    assert outcome["suggested_publish_time"] is None
    assert outcome["schedule_check"]["available"] is False
    assert outcome["pending_items"]


@pytest.mark.parametrize("value", ["false", "true", 0, 1, "0", [], {}])
def test_invalid_claim_types_are_rejected(value):
    reviewer = ReviewService()
    material = MaterialService(reviewer).read("M01").model_dump()
    material["review_metadata"] = {"claims_verified": value}
    with pytest.raises(ValidationError):
        Material.model_validate(material)


@pytest.mark.parametrize("metadata", [{"claims_verified": False}, {"claims_verified": None},
    {"has_promotional_claim": True}, {"has_promotional_claim": True, "claims_verified": None}])
def test_unknown_or_unverified_claim_needs_review(metadata, schedule_request):
    reviewer = ReviewService()
    material = MaterialService(reviewer).read("M01").model_dump()
    material["review_metadata"] = metadata
    assert reviewer.check(Material.model_validate(material), schedule_request).review_status == "NEEDS_REVIEW"


def test_no_claim_metadata_survives_round_trip(schedule_request):
    reviewer = ReviewService()
    material = MaterialService(reviewer).read("M01").model_dump()
    material["review_metadata"] = {"source": "synthetic_fixture"}
    parsed = Material.model_validate(material)
    assert reviewer.check(Material.model_validate_json(parsed.model_dump_json()), schedule_request).review_status == "PASS"


def test_conflict_confirmation_reaches_final_output(request_data, registry_factory, tmp_path):
    request = ScheduleRequest.model_validate({**request_data, "requested_publish_time": "2027-01-15T18:00:00-05:00"})
    args = {"material_id": "M01", "reason_summary": "Controlled regression"}
    model = ScriptedModel([action("read_material", json.dumps(args), "a"),
                           action("check_review_rules", json.dumps(args), "b"),
                           action("query_schedule", json.dumps({**args, "publish_time": request.requested_publish_time.isoformat()}), "c"),
                           {"role": "assistant", "content": "Please authorize a flexible time window."}])
    result = AgentRuntime(model, registry_factory(request), TraceRecorder(tmp_path, "confirmation", "mock", model.model_name)).run()
    outcome = result["outcomes"][0]
    assert result["execution_status"] == "COMPLETED"
    assert outcome["status"] == "NEEDS_CONFIRMATION"
    assert outcome["review"]["review_status"] == "PASS"
    assert "flexible_time_window" in " ".join(outcome["pending_items"])
    assert outcome["schedule_blockers"][0]["code"] == "REQUESTED_SLOT_UNAVAILABLE"
    assert outcome["schedule_blockers"][0]["conflicts"]
    assert outcome["draft"] is None


def test_no_change_lookup_keeps_review(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    prepare(registry)
    call(registry, "lookup_material_info")
    assert registry.outcomes()[0]["review"]["review_status"] == "PASS"


def test_explicit_schedule_sync_moves_and_cancels_atomically(tmp_path):
    path = tmp_path / "schedules.json"
    record = {"schedule_id": "TEST-SYNC", "target_channel": "TikTok", "market": "US", "publish_time": OLD, "status": "CONFIRMED"}
    path.write_text(json.dumps([record]), encoding="utf-8")
    repository = DraftRepository(tmp_path / "sync.db", path)
    record["publish_time"] = NEW
    path.write_text(json.dumps([record]), encoding="utf-8")
    assert repository.sync_schedules(path)["updated"] == 1
    record["status"] = "CANCELLED"
    path.write_text(json.dumps([record]), encoding="utf-8")
    assert repository.sync_schedules(path)["updated"] == 1
    with closing(repository.connect()) as connection:
        row = connection.execute("SELECT * FROM formal_schedules WHERE schedule_id='TEST-SYNC'").fetchone()
        assert row["status"] == "CANCELLED"
        assert datetime.fromisoformat(row["publish_time"]) == datetime.fromisoformat(NEW)
    path.write_text(json.dumps([{**record, "status": "CONFIRMED"}, {**record, "schedule_id": "INVALID", "publish_time": "2027-01-15T18:00:00"}]), encoding="utf-8")
    with pytest.raises(ValidationError):
        repository.sync_schedules(path)
    with closing(repository.connect()) as connection:
        assert connection.execute("SELECT status FROM formal_schedules WHERE schedule_id='TEST-SYNC'").fetchone()[0] == "CANCELLED"


def test_standalone_schedule_sync_cli(tmp_path, capsys):
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps([{"schedule_id": "CLI", "target_channel": "TikTok", "market": "US", "publish_time": NEW, "status": "CONFIRMED"}]), encoding="utf-8")
    assert main(["--sync-schedules", str(path), "--db", str(tmp_path / "cli.db"), "--output-dir", str(tmp_path / "traces")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schedule_sync"]["status"] == "SYNCHRONIZED"
    assert result["schedule_sync"]["inserted"] == 1
    assert result["schedule_sync"]["updated"] == 0
    assert result["execution_mode"] == "maintenance"
    assert result["tool_calls"] == 0


def test_candidate_boundaries_are_unverified_not_selected(request_data, registry_factory):
    registry = registry_factory(conflict_request(request_data))
    prepare(registry)
    result = call(registry, "query_schedule", publish_time="2027-01-15T19:05:00-05:00")
    hints = result["candidate_time_hints"]
    assert OLD in {h["publish_time"] for h in hints}
    assert NEW in {h["publish_time"] for h in hints}
    assert all(h["availability"] == "UNVERIFIED" for h in hints)
    assert registry.repository.all() == []


def test_candidate_boundaries_follow_changed_occupancy(request_data, registry_factory, tmp_path):
    registry = registry_factory(conflict_request(request_data))
    path = tmp_path / "moved.json"
    path.write_text(json.dumps([{"schedule_id": "SYN-US-1800", "target_channel": "TikTok", "market": "US",
                                  "publish_time": "2027-01-15T18:30:00-05:00", "status": "CONFIRMED"}]), encoding="utf-8")
    registry.repository.sync_schedules(path)
    _, result = prepare(registry)
    hints = {h["publish_time"] for h in result["candidate_time_hints"]}
    assert "2027-01-15T19:30:00-05:00" in hints
    assert OLD not in hints
