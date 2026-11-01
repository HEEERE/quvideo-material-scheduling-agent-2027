from datetime import datetime

import pytest
from pydantic import ValidationError

from src.models import ScheduleRequest


@pytest.mark.parametrize("patch", [
    {"requested_publish_time": "2027-01-15T16:00:00"},
    {"timezone": "invalid/zone"},
    {"requested_publish_time": "2027-01-15T16:00:00+09:00"},
    {"material_ids": ["M01", "M01"]},
    {"material_ids": []},
    {"unexpected": "cannot override"},
    {"flexible_time_window": {"start": "2027-01-16T18:00:00-05:00", "end": "2027-01-15T18:00:00-05:00"}},
    {"requested_publish_time": "2027-03-14T02:30:00-05:00"},
])
def test_reject_invalid_request(request_data, patch):
    with pytest.raises(ValidationError):
        ScheduleRequest.model_validate({**request_data, **patch})


def test_normalized_fingerprint_stable_for_order_and_object_keys(request_data):
    data = {**request_data, "material_ids": ["M01", "M07"]}
    first = ScheduleRequest.model_validate(data)
    second = ScheduleRequest.model_validate({**dict(reversed(list(data.items()))), "material_ids": ["M07", "M01"]})
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() == ScheduleRequest.model_validate_json(first.model_dump_json()).fingerprint()


@pytest.mark.parametrize("patch", [{"request_id": "other"}, {"hard_time_constraint": False},
                                   {"requested_publish_time": "2027-01-15T17:00:00-05:00"}, {"material_ids": ["M07"]}])
def test_different_user_needs_have_distinct_fingerprints(request_data, patch):
    assert ScheduleRequest.model_validate(request_data).fingerprint() != ScheduleRequest.model_validate({**request_data, **patch}).fingerprint()
