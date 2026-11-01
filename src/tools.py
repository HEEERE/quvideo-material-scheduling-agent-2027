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
    "save_schedule_draft": (SaveArgs, "Save reviewed, queried draft transactionally. For unchanged time use ORIGINAL_REQUEST; hard-time alternative requires PENDING unless explicit input confirmation grants USER_CONFIRMED; soft alternative inside window uses WITHIN_FLEXIBILITY. An invalid pending suggestion may be replanned under the same draft_id after querying a new legal time. Valid existing drafts return ALREADY_EXISTS; invalid approved drafts require user action. Returns actual SAVED/ALREADY_EXISTS/SAVE_FAILED/REJECTED_BY_POLICY. Never publishes."),
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
        self.schedule_observations: dict[str, dict[str, dict]] = {}
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
                if mid not in self.loaded or self.loaded[mid].model_dump() != material.model_dump():
                    self.reviews.pop(mid, None)
                self.loaded[mid] = material
                return {"status": "OK", "material": material.model_dump(mode="json")}
            if mid not in self.loaded:
                return {"status": "REJECTED_BY_POLICY", "error": "READ_MATERIAL_FIRST"}
            if name == "lookup_material_info":
                updated, record = self.materials.lookup(self.loaded[mid])
                if updated.model_dump() != self.loaded[mid].model_dump():
                    self.reviews.pop(mid, None)
                self.loaded[mid] = updated
                return {**record, "material": self.loaded[mid].model_dump(mode="json")}
            if name == "check_review_rules":
                review = self.reviewer.check(self.loaded[mid], self.request)
                self.reviews[mid] = review
                return review.model_dump(mode="json")
            if name == "query_schedule":
                result = self.repository.query(self.scheduler, self.request, mid, args.publish_time)
                self.queried.setdefault(mid, set()).add(utc_iso(args.publish_time))
                self.schedule_observations.setdefault(mid, {})[utc_iso(args.publish_time)] = result
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

    def _schedule_blockers(self, mid: str, saved: dict, current_slot: dict | None) -> list[dict]:
        if current_slot and not current_slot["available"]:
            return [{"code": "EXISTING_DRAFT_CONFLICT", "publish_time": current_slot["publish_time"],
                     "conflicts": current_slot["conflicts"], "requires_user_input": True,
                     "message": "The persisted draft time is now occupied. Replan a pending suggestion or explicitly confirm a new available time."}]
        reason = saved.get("error", "Policy rejected the draft")
        policy_blockers = ([{"code": "SCHEDULE_POLICY_REJECTED", "requires_user_input": reason.startswith("EXISTING_DRAFT_INVALID"),
                            "message": reason}] if saved.get("status") == "REJECTED_BY_POLICY" else [])
        observations = self.schedule_observations.get(mid, {})
        original = observations.get(utc_iso(self.request.requested_publish_time))
        if original and not original["available"]:
            has_confirmation = any(c.material_id == mid for c in self.request.user_confirmations)
            needs_window = self.request.flexible_time_window is None and not has_confirmation
            if needs_window:
                return [{"code": "REQUESTED_SLOT_UNAVAILABLE", "publish_time": original["publish_time"],
                         "conflicts": original["conflicts"], "requires_user_input": True,
                         "requested_fields": ["flexible_time_window", "user_confirmations"],
                         "message": "Requested publish time is occupied. Provide a flexible_time_window or explicit user_confirmations for an available alternative; the hard requirement remains unchanged."}] + policy_blockers
            if policy_blockers:
                return policy_blockers
            available = [slot["publish_time"] for slot in observations.values() if slot["available"]]
            return [{"code": "ALTERNATIVE_NOT_SAVED" if available else "NO_AVAILABLE_QUERIED_SLOT",
                     "publish_time": original["publish_time"], "conflicts": original["conflicts"],
                     "queried_available_times": available, "requires_user_input": False,
                     "message": "The requested time conflicts. Query and save a permitted alternative, or ask to expand the window if further searching cannot resolve the conflict."}]
        return policy_blockers

    def outcomes(self) -> list[dict]:
        outcomes = []
        for mid in self.request.material_ids:
            review = self.reviews.get(mid)
            saved = self.save_results.get(mid, {})
            draft = None
            verification_error = None
            schedule_check = None
            if saved.get("status") in {"SAVED", "ALREADY_EXISTS"}:
                # Output success only with a matching committed record.
                try:
                    draft = self.repository.get(self.request.fingerprint(), mid)
                    if draft and draft["draft_id"] == saved["draft_id"]:
                        schedule_check = self.repository.query(self.scheduler, self.request, mid,
                                                              datetime.fromisoformat(draft["suggested_publish_time"]))
                except Exception as exc:
                    # Loss of storage access cannot be reported as verified success.
                    draft = None
                    verification_error = {"code": "DATABASE_VERIFICATION_FAILED", "type": type(exc).__name__}
                status = "DRAFT_SAVED" if draft and draft["draft_id"] == saved["draft_id"] and not verification_error else "INCOMPLETE"
            elif saved.get("status") == "SAVE_FAILED":
                status = "SAVE_FAILED"
            elif review and review.review_status != "PASS":
                status = review.review_status
            elif mid in self.not_found:
                status = "NOT_FOUND"
            else:
                status = "INCOMPLETE"
            current_draft_valid = status == "DRAFT_SAVED" and schedule_check and schedule_check["available"]
            blockers = self._schedule_blockers(mid, saved, schedule_check) if not current_draft_valid and status not in {"NEEDS_INFO", "NEEDS_REVIEW", "BLOCKED", "NOT_FOUND"} else []
            if status != "SAVE_FAILED" and verification_error is None and any(b["requires_user_input"] for b in blockers):
                status = "NEEDS_CONFIRMATION"
            # A successful, still-valid pending draft needs confirmation of its exact time.
            if status == "DRAFT_SAVED":
                blockers = []
            pending = [b["message"] for b in blockers if b["requires_user_input"]]
            if status == "DRAFT_SAVED" and draft["confirmation_status"] == "PENDING":
                pending.append("Explicit user confirmation for the alternative time")
            if review and review.review_status != "PASS":
                pending.extend(review.risk_reasons)
            outcomes.append({"material_id": mid, "status": status,
                             "review": review.model_dump(mode="json") if review else None,
                             "original_publish_time": self.request.requested_publish_time.isoformat(),
                             "suggested_publish_time": draft["suggested_publish_time"] if status == "DRAFT_SAVED" else None,
                             "pending_items": pending, "schedule_blockers": blockers, "schedule_check": schedule_check,
                             "save_status": saved.get("status", "NOT_ATTEMPTED"), "draft": draft,
                             "verification_error": verification_error})
        return outcomes
