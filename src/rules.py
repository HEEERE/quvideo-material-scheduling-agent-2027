from __future__ import annotations

import json
from pathlib import Path

from .models import Material, ReviewResult, ScheduleRequest

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class ReviewService:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.rules = json.loads((data_dir / "rules.json").read_text(encoding="utf-8"))

    def missing(self, material: Material) -> list[str]:
        return [name for name in self.rules["required_fields"] if not getattr(material, name)]

    def check(self, material: Material, request: ScheduleRequest) -> ReviewResult:
        missing = self.missing(material)
        reasons, matched = [], []
        blocked, manual = False, False

        def hit(rule: str, reason: str):
            matched.append(rule)
            reasons.append(reason)

        if missing:
            hit("METADATA_REQUIRED", "Missing required metadata: " + ", ".join(missing))
        if material.copyright_status == "PROHIBITED":
            blocked = True
            hit("LICENSE_PROHIBITED", "Synthetic license register explicitly prohibits use")
        elif material.copyright_status in (None, "UNKNOWN"):
            if "copyright_status" not in missing:
                missing.append("copyright_status")
            hit("LICENSE_UNKNOWN", "Reliable copyright authorization must be supplied")
        elif material.authorization_scope and f"{request.target_channel}:{request.market}" not in material.authorization_scope:
            blocked = True
            hit("LICENSE_SCOPE", "Authorization does not cover requested channel and market")
        if material.target_market and material.target_market != request.market:
            manual = True
            hit("MARKET_MISMATCH", "Material market differs from request")
        if material.target_channel and material.target_channel != request.target_channel:
            manual = True
            hit("CHANNEL_MISMATCH", "Material channel differs from request")
        if request.market not in self.rules["market_languages"]:
            manual = True
            hit("MARKET_UNCONFIGURED", "No demonstration localization rules for this market")
        elif material.language and material.language not in self.rules["market_languages"][request.market]:
            manual = True
            hit("LANGUAGE_MISMATCH", "Material language is not configured for this market")
        for tag in material.content_tags:
            if tag in self.rules["blocked_tags"] or tag in self.rules["channel_blocked_tags"].get(request.target_channel, []):
                blocked = True
                hit("CONTENT_PROHIBITED", f"Demonstration blocked tag: {tag}")
            elif tag in self.rules["manual_review_tags"]:
                manual = True
                hit("MANUAL_CONTENT_REVIEW", f"Human review required for tag: {tag}")
        metadata = material.review_metadata
        if metadata.claims_verified is False or (metadata.has_promotional_claim and metadata.claims_verified is not True):
            manual = True
            hit("CLAIM_UNVERIFIED", "Promotional claim lacks verification evidence")
        status = "BLOCKED" if blocked else "NEEDS_INFO" if missing else "NEEDS_REVIEW" if manual else "PASS"
        return ReviewResult(material_id=material.material_id, review_status=status,
                            risk_level="HIGH" if blocked else "MEDIUM" if missing or manual else "LOW",
                            risk_reasons=reasons, matched_rules=matched, missing_fields=sorted(set(missing)),
                            requires_manual_review=status != "PASS")


class MaterialService:
    def __init__(self, reviewer: ReviewService, data_dir: Path = DATA_DIR):
        self.reviewer = reviewer
        self.materials = {m["material_id"]: Material.model_validate(m) for m in json.loads((data_dir / "materials.json").read_text(encoding="utf-8"))}
        self.supplemental = json.loads((data_dir / "supplemental.json").read_text(encoding="utf-8"))

    def read(self, material_id: str) -> Material | None:
        material = self.materials.get(material_id)
        if material:
            material = material.model_copy(deep=True)
            material.missing_fields = self.reviewer.missing(material)
        return material

    def lookup(self, material: Material) -> tuple[Material, dict]:
        record = self.supplemental.get(material.material_id)
        if not record or not record.get("reliable"):
            return material, {"status": "UNAVAILABLE", "note": "No reliable supplementary source; ask user"}
        # This fixture is trusted data. A model can never supply a metadata patch.
        patch = {k: v for k, v in record["patch"].items() if k in {"copyright_status", "authorization_scope"}}
        updated = Material.model_validate({**material.model_dump(), **patch})
        updated.missing_fields = self.reviewer.missing(updated)
        return updated, {"status": "FOUND" if patch else "STILL_MISSING", "source": record["source"], "note": record["note"]}
