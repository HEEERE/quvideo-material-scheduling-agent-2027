from concurrent.futures import ThreadPoolExecutor

from src.models import ScheduleRequest
from conftest import call, prepare


def save(registry):
    prepare(registry)
    return call(registry, "save_schedule_draft", suggested_publish_time=registry.request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")


def test_duplicate_survives_reopening(schedule_request, registry_factory):
    first = save(registry_factory(schedule_request))
    reopened = registry_factory(schedule_request)
    second = save(reopened)
    assert first["status"] == "SAVED"
    assert second["status"] == "ALREADY_EXISTS"
    assert first["draft_id"] == second["draft_id"]
    assert len(reopened.repository.all()) == 1


def test_concurrent_duplicate_commits_once(schedule_request, registry_factory):
    registries = [registry_factory(schedule_request) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(save, registries))
    assert sum(r["status"] == "SAVED" for r in results) == 1
    assert sum(r["status"] == "ALREADY_EXISTS" for r in results) == 7
    assert len({r["draft_id"] for r in results}) == 1
    assert len(registries[0].repository.all()) == 1


def test_failure_rolls_back_and_future_retry_can_succeed(schedule_request, registry_factory):
    failed = registry_factory(schedule_request, failure=True)
    result = save(failed)
    assert result["status"] == "SAVE_FAILED"
    assert result["draft_id"] is None
    assert not failed.repository.all()
    assert save(registry_factory(schedule_request))["status"] == "SAVED"


def test_different_requests_are_not_merged(request_data, schedule_request, registry_factory):
    first = save(registry_factory(schedule_request))
    second_request = ScheduleRequest.model_validate({**request_data, "request_id": "independent", "requested_publish_time": "2027-01-16T16:00:00-05:00"})
    other = registry_factory(second_request)
    second = save(other)
    assert first["draft_id"] != second["draft_id"]
    assert len(other.repository.all()) == 2


def test_concurrent_different_requests_cannot_reserve_same_slot(request_data, registry_factory):
    registries = [registry_factory(ScheduleRequest.model_validate({**request_data, "request_id": f"independent-{i}"})) for i in range(6)]
    for registry in registries:
        prepare(registry)
    def attempt(registry):
        return call(registry, "save_schedule_draft", suggested_publish_time=registry.request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, registries))
    assert sum(r["status"] == "SAVED" for r in results) == 1
    assert sum(r["status"] == "REJECTED_BY_POLICY" for r in results) == 5
    assert len(registries[0].repository.all()) == 1
