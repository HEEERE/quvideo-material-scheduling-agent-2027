from contextlib import closing
from datetime import datetime

from src.models import ScheduleRequest
from conftest import call, prepare
from test_guard import conflict_request


def test_formal_conflicts_and_exact_window_boundary(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    call(registry, "read_material")
    assert not call(registry, "query_schedule", publish_time="2027-01-15T18:00:00-05:00")["available"]
    assert not call(registry, "query_schedule", publish_time="2027-01-15T18:59:59-05:00")["available"]
    assert call(registry, "query_schedule", publish_time="2027-01-15T19:00:00-05:00")["available"]
    # Same instant represented in UTC must give the same result.
    assert not call(registry, "query_schedule", publish_time="2027-01-15T23:00:00+00:00")["available"]


def test_ready_draft_reserves_but_pending_does_not(schedule_request, request_data, registry_factory):
    first = registry_factory(schedule_request)
    prepare(first)
    call(first, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    other = registry_factory(ScheduleRequest.model_validate({**request_data, "request_id": "other"}))
    prepare(other)
    assert not call(other, "query_schedule", publish_time="2027-01-15T16:00:00-05:00")["available"]
    pending = registry_factory(conflict_request(request_data))
    prepare(pending, time="2027-01-15T19:00:00-05:00")
    call(pending, "save_schedule_draft", suggested_publish_time="2027-01-15T19:00:00-05:00", confirmation_status="PENDING")
    assert call(other, "query_schedule", publish_time="2027-01-15T19:00:00-05:00")["available"]


def test_save_rechecks_after_concurrent_formal_insertion(schedule_request, registry_factory):
    registry = registry_factory(schedule_request)
    assert prepare(registry)[1]["available"]
    with closing(registry.repository.connect()) as connection:
        connection.execute("INSERT INTO formal_schedules VALUES(?,?,?,?,?)", ("racing", "TikTok", "US", "2027-01-15T21:00:00+00:00", "CONFIRMED"))
        connection.commit()
    result = call(registry, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    assert result["status"] == "REJECTED_BY_POLICY"
    assert "SLOT_CHANGED" in result["error"]
    assert not registry.repository.all()


def test_market_channel_isolation_and_cancelled_slot(request_data, registry_factory):
    request = ScheduleRequest.model_validate({**request_data, "material_ids": ["M04"], "market": "JP", "target_channel": "Instagram Reels", "timezone": "Asia/Tokyo", "requested_publish_time": "2027-01-15T18:00:00+09:00"})
    registry = registry_factory(request)
    assert prepare(registry, "M04")[1]["available"]
