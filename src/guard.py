from __future__ import annotations

from datetime import datetime, timezone

from .models import DraftProposal, ReviewResult, ScheduleRequest


class PolicyViolation(ValueError):
    pass


class PolicyGuard:
    def validate(self, request: ScheduleRequest, proposal: DraftProposal, review: ReviewResult,
                 queried_times: set[str], current_slot: dict) -> str:
        if proposal.material_id not in request.material_ids or proposal.material_id != review.material_id:
            raise PolicyViolation("MATERIAL_OUTSIDE_REQUEST")
        if review.review_status != "PASS":
            raise PolicyViolation(f"REVIEW_NOT_PASS: {review.review_status}")
        target = proposal.suggested_publish_time
        if target <= datetime.now(timezone.utc):
            raise PolicyViolation("TARGET_TIME_IS_PAST")
        if target.astimezone(timezone.utc).isoformat() not in queried_times:
            raise PolicyViolation("MODEL_MUST_QUERY_TARGET_TIME_FIRST")
        explicit = any(c.material_id == proposal.material_id and c.target_time == target for c in request.user_confirmations)
        if explicit:
            expected = "USER_CONFIRMED"
        elif target == request.requested_publish_time:
            expected = "ORIGINAL_REQUEST"
        else:
            window = request.flexible_time_window
            if not window or not (window.start <= target <= window.end):
                raise PolicyViolation("ALTERNATIVE_OUTSIDE_USER_WINDOW")
            expected = "PENDING" if request.hard_time_constraint else "WITHIN_FLEXIBILITY"
        if proposal.confirmation_status != expected:
            raise PolicyViolation(f"CONFIRMATION_MISMATCH: expected {expected}; model cannot grant approval")
        if not current_slot["available"]:
            raise PolicyViolation("SLOT_CHANGED_OR_CONFLICTING: query a different time")
        return "AWAITING_CONFIRMATION" if expected == "PENDING" else "READY"
