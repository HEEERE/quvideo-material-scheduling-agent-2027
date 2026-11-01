from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Explicit ISO 8601 UTC offset is required")
    return value


def utc_iso(value: datetime) -> str:
    return aware(value).astimezone(timezone.utc).isoformat()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewMetadata(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)
    has_promotional_claim: StrictBool = False
    claims_verified: StrictBool | None = None

    @model_validator(mode="before")
    @classmethod
    def identify_claim(cls, value):
        if isinstance(value, dict) and "claims_verified" in value and "has_promotional_claim" not in value:
            return {**value, "has_promotional_claim": True}
        return value


class Material(Record):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    material_id: str
    title: str | None = None
    material_type: str | None = None
    content_theme: str | None = None
    target_product: str | None = None
    target_market: str | None = None
    language: str | None = None
    target_channel: str | None = None
    copyright_status: Literal["AUTHORIZED", "UNKNOWN", "PROHIBITED"] | None = None
    authorization_scope: list[str] = Field(default_factory=list)
    content_tags: list[str] = Field(default_factory=list)
    review_metadata: ReviewMetadata = Field(default_factory=ReviewMetadata)
    missing_fields: list[str] = Field(default_factory=list)

    @field_validator("material_id", "title", "material_type", "content_theme", "target_product", "target_market", "language", "target_channel")
    @classmethod
    def strip_metadata(cls, value):
        return value.strip() if isinstance(value, str) else value


class TimeWindow(Record):
    start: datetime
    end: datetime

    _aware = field_validator("start", "end")(aware)

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end or (self.end - self.start).total_seconds() > 7 * 86400:
            raise ValueError("Window must be ordered and no longer than seven days")
        return self


class UserConfirmation(Record):
    material_id: str = Field(min_length=1)
    target_time: datetime
    confirmed_at: datetime
    source: Literal["explicit_user_input"]

    _aware = field_validator("target_time", "confirmed_at")(aware)

    @model_validator(mode="after")
    def chronological(self):
        if self.confirmed_at > self.target_time:
            raise ValueError("Confirmation must precede its target time")
        if self.confirmed_at > datetime.now(timezone.utc):
            raise ValueError("Confirmation cannot be dated in the future")
        return self


class ScheduleRequest(Record):
    request_id: str = Field(min_length=1, max_length=100)
    material_ids: list[str] = Field(min_length=1, max_length=10)
    target_channel: Literal["TikTok", "Instagram Reels", "YouTube Shorts", "InApp"]
    market: str = Field(min_length=2, max_length=10)
    timezone: str
    requested_publish_time: datetime
    hard_time_constraint: bool = True
    flexible_time_window: TimeWindow | None = None
    user_confirmations: list[UserConfirmation] = Field(default_factory=list)

    _aware = field_validator("requested_publish_time")(aware)

    @model_validator(mode="after")
    def validate_context(self):
        try:
            zone = ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Valid IANA timezone required") from exc
        if len(set(self.material_ids)) != len(self.material_ids) or any(not x.strip() for x in self.material_ids):
            raise ValueError("material_ids must be nonempty and unique")
        dates = [self.requested_publish_time]
        if self.flexible_time_window:
            dates += [self.flexible_time_window.start, self.flexible_time_window.end]
        for confirmation in self.user_confirmations:
            if confirmation.material_id not in self.material_ids:
                raise ValueError("Confirmation material is outside this request")
            dates.append(confirmation.target_time)
        for date in dates:
            if date.utcoffset() != date.astimezone(zone).utcoffset():
                raise ValueError("UTC offset does not match the IANA timezone at this instant")
        return self

    def fingerprint(self) -> str:
        data = self.model_dump(mode="json")
        data["material_ids"] = sorted(self.material_ids)
        data["requested_publish_time"] = utc_iso(self.requested_publish_time)
        if self.flexible_time_window:
            data["flexible_time_window"] = {k: utc_iso(v) for k, v in self.flexible_time_window.model_dump().items()}
        data["user_confirmations"] = sorted(
            [{**c.model_dump(mode="json"), "target_time": utc_iso(c.target_time),
              "confirmed_at": utc_iso(c.confirmed_at)} for c in self.user_confirmations],
            key=lambda c: (c["material_id"], c["target_time"], c["confirmed_at"]),
        )
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


ReviewStatus = Literal["PASS", "NEEDS_INFO", "NEEDS_REVIEW", "BLOCKED"]
ConfirmationStatus = Literal["ORIGINAL_REQUEST", "WITHIN_FLEXIBILITY", "USER_CONFIRMED", "PENDING"]


class ReviewResult(Record):
    material_id: str
    review_status: ReviewStatus
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    risk_reasons: list[str]
    matched_rules: list[str]
    missing_fields: list[str]
    requires_manual_review: bool


class DraftProposal(Record):
    material_id: str
    suggested_publish_time: datetime
    confirmation_status: ConfirmationStatus

    _aware = field_validator("suggested_publish_time")(aware)


class ScheduleDraft(Record):
    draft_id: str
    request_fingerprint: str
    material_id: str
    original_publish_time: datetime
    suggested_publish_time: datetime
    target_channel: str
    market: str
    timezone: str
    confirmation_status: ConfirmationStatus
    review_status: ReviewStatus
    draft_status: Literal["READY", "AWAITING_CONFIRMATION"]
    risk_reasons: list[str]
    confirmation_evidence: list[UserConfirmation]
    created_at: datetime
    updated_at: datetime
    revision: int = Field(default=1, ge=1)

    _aware = field_validator("original_publish_time", "suggested_publish_time", "created_at", "updated_at")(aware)


class FormalSchedule(Record):
    schedule_id: str = Field(min_length=1)
    target_channel: Literal["TikTok", "Instagram Reels", "YouTube Shorts", "InApp"]
    market: str = Field(min_length=2, max_length=10)
    publish_time: datetime
    status: Literal["CONFIRMED", "CANCELLED"]

    _aware = field_validator("publish_time")(aware)
