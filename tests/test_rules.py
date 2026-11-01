import pytest

from src.models import ScheduleRequest
from src.rules import MaterialService, ReviewService


@pytest.mark.parametrize("mid,status", [("M01", "PASS"), ("M02", "NEEDS_INFO"),
                                         ("M03", "BLOCKED"), ("M05", "NEEDS_REVIEW")])
def test_synthetic_cases(schedule_request, mid, status):
    reviewer = ReviewService()
    result = reviewer.check(MaterialService(reviewer).read(mid), schedule_request)
    assert result.review_status == status
    assert result.requires_manual_review == (status != "PASS")


def test_localization(request_data):
    reviewer = ReviewService()
    request = ScheduleRequest.model_validate({**request_data, "material_ids": ["M06"], "market": "JP",
                 "target_channel": "YouTube Shorts", "timezone": "Asia/Tokyo", "requested_publish_time": "2027-01-15T16:00:00+09:00"})
    result = reviewer.check(MaterialService(reviewer).read("M06"), request)
    assert result.review_status == "NEEDS_REVIEW"
    assert "LANGUAGE_MISMATCH" in result.matched_rules


def test_license_scope_and_missing_metadata(schedule_request):
    reviewer = ReviewService()
    material = MaterialService(reviewer).read("M01")
    material.authorization_scope = ["InApp:US"]
    assert reviewer.check(material, schedule_request).review_status == "BLOCKED"
    material.authorization_scope = ["TikTok:US"]
    material.title = None
    result = reviewer.check(material, schedule_request)
    assert result.review_status == "NEEDS_INFO"
    assert "title" in result.missing_fields


def test_lookup_reliable_source_and_unavailable(schedule_request):
    reviewer = ReviewService()
    materials = MaterialService(reviewer)
    updated, evidence = materials.lookup(materials.read("M07"))
    assert evidence["source"] == "synthetic_license_register"
    assert reviewer.check(updated, schedule_request).review_status == "PASS"
    updated, evidence = materials.lookup(materials.read("M02"))
    assert evidence["status"] == "STILL_MISSING"
    assert reviewer.check(updated, schedule_request).review_status == "NEEDS_INFO"


def test_blank_title_does_not_pass(schedule_request):
    from src.models import Material
    reviewer = ReviewService()
    data = MaterialService(reviewer).read("M01").model_dump()
    data["title"] = "  "
    assert reviewer.check(Material.model_validate(data), schedule_request).review_status == "NEEDS_INFO"
