"""Current review evidence and batch suggestions must agree with final outcomes."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import call, prepare
from src.main import run_case
from test_guard import conflict_request


TARGET = "2027-01-15T19:00:00-05:00"
ALTERNATIVE = "2027-01-15T21:00:00-05:00"


def save_original(registry):
    prepare(registry)
    result = call(registry, "save_schedule_draft",
                  suggested_publish_time=registry.request.requested_publish_time.isoformat(),
                  confirmation_status="ORIGINAL_REQUEST")
    assert result["status"] == "SAVED"
    return result


def prepare_batch_material(registry, mid):
    call(registry, "read_material", mid)
    if mid == "M07":
        call(registry, "lookup_material_info", mid)
    assert call(registry, "check_review_rules", mid)["review_status"] == "PASS"
    return call(registry, "query_schedule", mid, publish_time=TARGET)


def save_pending(registry, mid, target=TARGET):
    return call(registry, "save_schedule_draft", mid,
                suggested_publish_time=target, confirmation_status="PENDING")


def test_batch_pending_suggestions_cannot_overlap(request_data, registry_factory):
    registry = registry_factory(conflict_request(request_data, material_ids=["M01", "M07"]))
    assert prepare_batch_material(registry, "M01")["available"]
    first = save_pending(registry, "M01")
    assert first["status"] == "SAVED"
    slot = prepare_batch_material(registry, "M07")
    assert not slot["available"]
    assert slot["conflicts"][0]["id"] == first["draft_id"]
    assert save_pending(registry, "M07")["status"] == "REJECTED_BY_POLICY"
    assert call(registry, "query_schedule", "M07", publish_time=ALTERNATIVE)["available"]
    assert save_pending(registry, "M07", ALTERNATIVE)["status"] == "SAVED"
    assert all(o["schedule_check"]["available"] for o in registry.outcomes())
    assert all(o["draft"]["confirmation_status"] == "PENDING" for o in registry.outcomes())


def test_concurrent_pending_suggestions_recheck_batch_conflicts(request_data, registry_factory):
    request = conflict_request(request_data, material_ids=["M01", "M07"])
    registries = [registry_factory(request), registry_factory(request)]
    pairs = list(zip(registries, request.material_ids))
    for registry, mid in pairs:
        assert prepare_batch_material(registry, mid)["available"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: save_pending(*pair), pairs))
    assert sorted(r["status"] for r in results) == ["REJECTED_BY_POLICY", "SAVED"]
    assert len(registries[0].repository.all()) == 1


def test_mock_batch_chooses_distinct_pending_times(request_data, tmp_path):
    request = conflict_request(request_data, material_ids=["M01", "M07"])
    result = run_case(request, mode="mock", db_path=tmp_path / "batch.db",
                      output_dir=tmp_path / "traces", max_steps=30, max_tool_calls=40)
    assert result["execution_status"] == "COMPLETED"
    assert all(o["status"] == "DRAFT_SAVED" for o in result["outcomes"])
    assert {o["suggested_publish_time"] for o in result["outcomes"]} == {TARGET, ALTERNATIVE}


@pytest.mark.parametrize("recheck", [False, True])
def test_changed_material_invalidates_saved_recommendation(schedule_request, registry_factory, recheck):
    registry = registry_factory(schedule_request)
    saved = save_original(registry)
    registry.materials.materials["M01"].copyright_status = "PROHIBITED"
    call(registry, "read_material")
    if recheck:
        assert call(registry, "check_review_rules")["review_status"] == "BLOCKED"
    outcome = registry.outcomes()[0]
    assert outcome["status"] == ("BLOCKED" if recheck else "INCOMPLETE")
    assert outcome["suggested_publish_time"] is None
    assert outcome["save_status"] == "SAVED"  # Historical persistence remains factual.
    assert registry.repository.all()[0]["draft_id"] == saved["draft_id"]


def test_save_rejection_replaces_previous_success_result(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    save_original(registry)
    registry.loaded["M01"].copyright_status = "PROHIBITED"
    result = call(registry, "save_schedule_draft",
                  suggested_publish_time=schedule_request.requested_publish_time.isoformat(),
                  confirmation_status="ORIGINAL_REQUEST")
    assert result["status"] == "REJECTED_BY_POLICY"
    outcome = registry.outcomes()[0]
    assert outcome["status"] == "BLOCKED"
    assert outcome["save_status"] == "REJECTED_BY_POLICY"
    assert outcome["save_error"]["message"] == "REVIEW_NOT_PASS"
    assert outcome["suggested_publish_time"] is None


def test_missing_reread_cannot_reuse_cached_material(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    save_original(registry)
    del registry.materials.materials["M01"]
    assert call(registry, "read_material")["status"] == "NOT_FOUND"
    assert call(registry, "check_review_rules")["status"] == "REJECTED_BY_POLICY"
    result = call(registry, "save_schedule_draft",
                  suggested_publish_time=schedule_request.requested_publish_time.isoformat(),
                  confirmation_status="ORIGINAL_REQUEST")
    assert result["status"] == "REJECTED_BY_POLICY"
    assert registry.outcomes()[0]["status"] == "NOT_FOUND"
    assert registry.outcomes()[0]["suggested_publish_time"] is None


def test_material_recovery_clears_not_found(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    material = registry.materials.materials.pop("M01")
    assert call(registry, "read_material")["status"] == "NOT_FOUND"
    registry.materials.materials["M01"] = material
    prepare(registry)
    assert registry.outcomes()[0]["status"] == "INCOMPLETE"
    assert registry.outcomes()[0]["review"]["review_status"] == "PASS"


@pytest.mark.parametrize("operation", ["read_material", "lookup_material_info", "check_review_rules", "save_schedule_draft"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_evidence_error_cannot_keep_previous_pass(schedule_request, registry_factory, monkeypatch, operation, error_type):
    registry = registry_factory(schedule_request)
    save_original(registry)

    def unavailable(*args):
        raise error_type("Evidence source unavailable")

    if operation == "read_material":
        monkeypatch.setattr(registry.materials, "read", unavailable)
    elif operation == "lookup_material_info":
        monkeypatch.setattr(registry.materials, "lookup", unavailable)
    else:
        monkeypatch.setattr(registry.reviewer, "check", unavailable)
    args = ({"suggested_publish_time": schedule_request.requested_publish_time.isoformat(),
             "confirmation_status": "ORIGINAL_REQUEST"} if operation == "save_schedule_draft" else {})
    error_status = "INVALID_ARGUMENTS" if error_type is ValueError else "TOOL_ERROR"
    assert call(registry, operation, **args)["status"] == error_status
    outcome = registry.outcomes()[0]
    assert outcome["status"] == "INCOMPLETE"
    assert outcome["review"] is None
    assert outcome["review_error"]["tool_name"] == operation
    assert outcome["suggested_publish_time"] is None
    if operation == "save_schedule_draft":
        assert outcome["save_status"] == error_status

    # A fresh successful review and save retry recover without a duplicate draft.
    monkeypatch.undo()
    prepare(registry)
    repeated = call(registry, "save_schedule_draft",
                    suggested_publish_time=schedule_request.requested_publish_time.isoformat(),
                    confirmation_status="ORIGINAL_REQUEST")
    assert repeated["status"] == "ALREADY_EXISTS"
    recovered = registry.outcomes()[0]
    assert recovered["status"] == "DRAFT_SAVED"
    assert recovered["review_error"] is None
    assert recovered["save_error"] is None
    assert len(registry.repository.all()) == 1


def test_final_outcome_preserves_save_failure_reason(schedule_request, registry_factory):
    registry = registry_factory(schedule_request, failure=True)
    prepare(registry)
    call(registry, "save_schedule_draft",
         suggested_publish_time=schedule_request.requested_publish_time.isoformat(),
         confirmation_status="ORIGINAL_REQUEST")
    outcome = registry.outcomes()[0]
    assert outcome["status"] == "SAVE_FAILED"
    assert outcome["save_error"]["code"] == "INJECTED_COMMIT_FAILURE"
    assert not registry.repository.all()
