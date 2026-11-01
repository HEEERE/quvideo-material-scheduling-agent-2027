from datetime import datetime, timedelta, timezone

import pytest

from src.models import ScheduleRequest
from conftest import call, prepare


def conflict_request(request_data, **patch):
    return ScheduleRequest.model_validate({**request_data, "requested_publish_time": "2027-01-15T18:00:00-05:00",
        "flexible_time_window": {"start": "2027-01-15T18:00:00-05:00", "end": "2027-01-15T21:00:00-05:00"}, **patch})


@pytest.mark.parametrize("claimed", ["ORIGINAL_REQUEST", "WITHIN_FLEXIBILITY", "USER_CONFIRMED"])
def test_hard_time_alternative_cannot_self_approve(request_data, registry_factory, claimed):
    registry = registry_factory(conflict_request(request_data))
    prepare(registry, time="2027-01-15T19:00:00-05:00")
    result = call(registry, "save_schedule_draft", suggested_publish_time="2027-01-15T19:00:00-05:00", confirmation_status=claimed)
    assert result["status"] == "REJECTED_BY_POLICY"
    assert not registry.repository.all()


def test_pending_alternative_keeps_original(request_data, registry_factory):
    registry = registry_factory(conflict_request(request_data))
    prepare(registry, time="2027-01-15T19:00:00-05:00")
    result = call(registry, "save_schedule_draft", suggested_publish_time="2027-01-15T19:00:00-05:00", confirmation_status="PENDING")
    assert result["status"] == "SAVED"
    assert result["draft"]["draft_status"] == "AWAITING_CONFIRMATION"
    assert datetime.fromisoformat(result["draft"]["original_publish_time"]) == registry.request.requested_publish_time
    assert result["draft"]["confirmation_evidence"] == []


def test_explicit_user_confirmation_is_persisted(request_data, registry_factory):
    confirmation = {"material_id": "M01", "target_time": "2027-01-15T19:00:00-05:00",
                    "confirmed_at": (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(), "source": "explicit_user_input"}
    registry = registry_factory(conflict_request(request_data, user_confirmations=[confirmation]))
    prepare(registry, time=confirmation["target_time"])
    result = call(registry, "save_schedule_draft", suggested_publish_time=confirmation["target_time"], confirmation_status="USER_CONFIRMED")
    assert result["status"] == "SAVED"
    assert result["draft"]["draft_status"] == "READY"
    assert result["draft"]["confirmation_evidence"][0]["source"] == "explicit_user_input"


def test_soft_alternative_and_window_enforced(request_data, registry_factory):
    registry = registry_factory(conflict_request(request_data, hard_time_constraint=False))
    prepare(registry, time="2027-01-15T19:00:00-05:00")
    assert call(registry, "save_schedule_draft", suggested_publish_time="2027-01-15T19:00:00-05:00", confirmation_status="WITHIN_FLEXIBILITY")["status"] == "SAVED"
    other = registry_factory(conflict_request(request_data, request_id="out-of-window"))
    prepare(other, time="2027-01-15T22:00:00-05:00")
    assert call(other, "save_schedule_draft", suggested_publish_time="2027-01-15T22:00:00-05:00", confirmation_status="PENDING")["status"] == "REJECTED_BY_POLICY"


def test_save_requires_observed_review_and_query(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    args = dict(suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    assert call(registry, "save_schedule_draft", **args)["status"] == "REJECTED_BY_POLICY"
    call(registry, "read_material")
    assert call(registry, "save_schedule_draft", **args)["status"] == "REJECTED_BY_POLICY"
    call(registry, "check_review_rules")
    assert call(registry, "save_schedule_draft", **args)["status"] == "REJECTED_BY_POLICY"
    assert not registry.repository.all()


@pytest.mark.parametrize("mid", ["M02", "M03", "M05"])
def test_non_pass_review_never_saved(request_data, registry_factory, mid):
    registry = registry_factory(ScheduleRequest.model_validate({**request_data, "material_ids": [mid]}))
    prepare(registry, mid)
    result = call(registry, "save_schedule_draft", mid, suggested_publish_time=registry.request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    assert result["status"] == "REJECTED_BY_POLICY"
    assert not registry.repository.all()


def test_model_cannot_supply_request_override(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    prepare(registry)
    result = call(registry, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST", hard_time_constraint=False)
    assert result["status"] == "INVALID_ARGUMENTS"
    assert call(registry, "read_material", "M03")["status"] == "REJECTED_BY_POLICY"
