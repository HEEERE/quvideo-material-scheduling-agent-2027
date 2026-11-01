import json
from pathlib import Path

import pytest

from src.models import ScheduleRequest
from src.rules import MaterialService, ReviewService
from src.scheduler import ScheduleService
from src.storage import DraftRepository
from src.tools import ToolRegistry


@pytest.fixture
def request_data():
    return json.loads((Path(__file__).resolve().parents[1] / "examples" / "normal.input.json").read_text(encoding="utf-8"))


@pytest.fixture
def schedule_request(request_data):
    return ScheduleRequest.model_validate(request_data)


@pytest.fixture
def registry_factory(tmp_path):
    def factory(request, failure=False, db_name="test.db"):
        reviewer = ReviewService()
        return ToolRegistry(request, DraftRepository(tmp_path / db_name), reviewer,
                            MaterialService(reviewer), ScheduleService(60), failure)
    return factory


def call(registry, name, mid="M01", **kwargs):
    return registry.call(name, json.dumps({"material_id": mid, "reason_summary": "Controlled engineering test", **kwargs}))


def prepare(registry, mid="M01", time=None):
    call(registry, "read_material", mid)
    review = call(registry, "check_review_rules", mid)
    slot = call(registry, "query_schedule", mid, publish_time=time or registry.request.requested_publish_time.isoformat())
    return review, slot
