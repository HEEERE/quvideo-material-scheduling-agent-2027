from __future__ import annotations

import json
from datetime import datetime

from pydantic import Field, ValidationError, field_validator

from .guard import PolicyGuard
from .models import ConfirmationStatus, DraftProposal, Material, Record, ScheduleRequest, aware, utc_iso
from .rules import MaterialService, ReviewService
from .scheduler import ScheduleService
from .storage import DraftRepository


class MaterialArgs(Record):
    material_id: str = Field(min_length=1, max_length=100)
    reason_summary: str = Field(min_length=1, max_length=240, description="Short public action rationale, never private chain of thought")


class QueryArgs(MaterialArgs):
    publish_time: datetime
    _aware = field_validator("publish_time")(aware)


class SaveArgs(MaterialArgs):
    suggested_publish_time: datetime
    confirmation_status: ConfirmationStatus
    _aware = field_validator("suggested_publish_time")(aware)


TOOL_PROTOCOLS = {
    "read_material": (MaterialArgs, "Read synthetic material by ID. Always read before reviewing. Returns missing fields."),
    "lookup_material_info": (MaterialArgs, "Look up missing authorization in a trusted synthetic register. May still return STILL_MISSING. Never invent license data; review again after lookup."),
    "check_review_rules": (MaterialArgs, "Deterministically review previously read material against the immutable original request. Only PASS allows a draft. NEEDS_INFO needs supplementary evidence; BLOCKED/NEEDS_REVIEW stop scheduling."),
    "query_schedule": (QueryArgs, "Query one exact ISO8601 time with offset, using immutable channel/market. Read material first. Observe conflicts, choose and query another time if necessary. No implicit approval of alternatives."),
    "save_schedule_draft": (SaveArgs, "Save reviewed, queried draft transactionally. For unchanged time use ORIGINAL_REQUEST; hard-time alternative requires PENDING unless explicit input confirmation grants USER_CONFIRMED; soft alternative inside window uses WITHIN_FLEXIBILITY. Returns actual SAVED/ALREADY_EXISTS/SAVE_FAILED/REJECTED_BY_POLICY. Never publishes."),
}


def tool_schemas() -> list[dict]:
    return [{"type": "function", "function": {"name": name, "description": description,
            "parameters": model.model_json_schema()}} for name, (model, description) in TOOL_PROTOCOLS.items()]


class ToolRegistry:
    def __init__(self, request: ScheduleRequest, repository: DraftRepository, reviewer: ReviewService,
                 materials: MaterialService, scheduler: ScheduleService, simulate_save_failure: bool = False):
        self.request = request
        self.repository, self.reviewer, self.materials, self.scheduler = repository, reviewer, materials, scheduler
        self.guard = PolicyGuard()
        self.simulate_save_failure = simulate_save_failure
        self.loaded: dict[str, Material] = {}
        self.reviews = {}
        self.queried: dict[str, set[str]] = {}
        self.save_results: dict[str, dict] = {}
        self.not_found: set[str] = set()

    def call(self, name: str, raw_arguments: str) -> dict:
        if name not in TOOL_PROTOCOLS:
            return {"status": "INVALID_TOOL", "error": "Unknown tool"}
        try:
            args = TOOL_PROTOCOLS[name][0].model_validate_json(raw_arguments)
        except (ValidationError, ValueError, TypeError):
            return {"status": "INVALID_ARGUMENTS", "error": "Arguments must match the tool schema, include explicit time offset and a short reason, and contain no extra fields"}
        mid = args.material_id
        if mid not in self.request.material_ids:
            return {"status": "REJECTED_BY_POLICY", "error": "MATERIAL_OUTSIDE_REQUEST"}
        try:
            if name == "read_material":
                material = self.materials.read(mid)
                if not material:
                    self.not_found.add(mid)
                    return {"status": "NOT_FOUND", "material_id": mid}
                self.loaded[mid] = material
                self.reviews.pop(mid, None)
                return {"status": "OK", "material": material.model_dump(mode="json")}
            if mid not in self.loaded:
                return {"status": "REJECTED_BY_POLICY", "error": "READ_MATERIAL_FIRST"}
            if name == "lookup_material_info":
                self.loaded[mid], record = self.materials.lookup(self.loaded[mid])
                self.reviews.pop(mid, None)
                return {**record, "material": self.loaded[mid].model_dump(mode="json")}
            if name == "check_review_rules":
                review = self.reviewer.check(self.loaded[mid], self.request)
                self.reviews[mid] = review
                return review.model_dump(mode="json")
            if name == "query_schedule":
                result = self.repository.query(self.scheduler, self.request, mid, args.publish_time)
                self.queried.setdefault(mid, set()).add(utc_iso(args.publish_time))
                return result
            if name == "save_schedule_draft":
                if mid not in self.reviews:
                    return {"status": "REJECTED_BY_POLICY", "draft_id": None, "error": "CHECK_REVIEW_RULES_FIRST"}
                # Re-evaluate authoritative data; never accept model-supplied review fields.
                review = self.reviewer.check(self.loaded[mid], self.request)
                self.reviews[mid] = review
                if review.review_status != "PASS":
                    return {"status": "REJECTED_BY_POLICY", "draft_id": None, "error": "REVIEW_NOT_PASS"}
                proposal = DraftProposal(material_id=mid, suggested_publish_time=args.suggested_publish_time,
                                         confirmation_status=args.confirmation_status)
                result = self.repository.save(self.request, proposal, review, self.queried.get(mid, set()),
                                              self.guard, self.scheduler, self.simulate_save_failure)
                self.save_results[mid] = result
                return result
        except ValueError as exc:
            return {"status": "INVALID_ARGUMENTS", "error": str(exc)}
        except Exception as exc:
            return {"status": "TOOL_ERROR", "error": type(exc).__name__}
        return {"status": "TOOL_ERROR", "error": "Unreachable dispatch"}

    def outcomes(self) -> list[dict]:
        outcomes = []
        for mid in self.request.material_ids:
            review = self.reviews.get(mid)
            saved = self.save_results.get(mid, {})
            draft = None
            verification_error = None
            if saved.get("status") in {"SAVED", "ALREADY_EXISTS"}:
                # Output success only with a matching committed record.
                try:
                    draft = self.repository.get(self.request.fingerprint(), mid)
                except Exception as exc:
                    # Loss of storage access cannot be reported as verified success.
                    draft = None
                    verification_error = {"code": "DATABASE_VERIFICATION_FAILED", "type": type(exc).__name__}
                status = "DRAFT_SAVED" if draft and draft["draft_id"] == saved["draft_id"] else "INCOMPLETE"
            elif saved.get("status") == "SAVE_FAILED":
                status = "SAVE_FAILED"
            elif review and review.review_status != "PASS":
                status = review.review_status
            elif mid in self.not_found:
                status = "NOT_FOUND"
            else:
                status = "INCOMPLETE"
            outcomes.append({"material_id": mid, "status": status,
                             "review": review.model_dump(mode="json") if review else None,
                             "original_publish_time": self.request.requested_publish_time.isoformat(),
                             "suggested_publish_time": draft["suggested_publish_time"] if draft else None,
                             "pending_items": (["Explicit user confirmation for the alternative time"] if draft and draft["confirmation_status"] == "PENDING" else []) + (review.risk_reasons if review and review.review_status != "PASS" else []),
                             "save_status": saved.get("status", "NOT_ATTEMPTED"), "draft": draft,
                             "verification_error": verification_error})
        return outcomes
