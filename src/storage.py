from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .guard import PolicyGuard, PolicyViolation
from .models import DraftProposal, ReviewResult, ScheduleDraft, ScheduleRequest, utc_iso
from .rules import DATA_DIR
from .scheduler import ScheduleService


class DraftRepository:
    def __init__(self, path: Path, schedules_path: Path = DATA_DIR / "schedules.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS formal_schedules (
                  schedule_id TEXT PRIMARY KEY, target_channel TEXT NOT NULL,
                  market TEXT NOT NULL, publish_time TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('CONFIRMED','CANCELLED')));
                CREATE TABLE IF NOT EXISTS drafts (
                  draft_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
                  material_id TEXT NOT NULL, target_channel TEXT NOT NULL, market TEXT NOT NULL,
                  publish_time TEXT NOT NULL,
                  draft_status TEXT NOT NULL CHECK(draft_status IN ('READY','AWAITING_CONFIRMATION')),
                  payload TEXT NOT NULL, UNIQUE(request_fingerprint,material_id));
                CREATE INDEX IF NOT EXISTS draft_scope ON drafts(target_channel,market,draft_status);
            """)
            for item in json.loads(schedules_path.read_text(encoding="utf-8")):
                aware_time = datetime.fromisoformat(item["publish_time"])
                connection.execute("INSERT OR IGNORE INTO formal_schedules VALUES(?,?,?,?,?)",
                                   (item["schedule_id"], item["target_channel"], item["market"], utc_iso(aware_time), item["status"]))
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def get(self, fingerprint: str, material_id: str, connection=None) -> dict | None:
        if connection is None:
            with closing(self.connect()) as conn:
                return self.get(fingerprint, material_id, conn)
        row = connection.execute("SELECT payload FROM drafts WHERE request_fingerprint=? AND material_id=?", (fingerprint, material_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def all(self) -> list[dict]:
        with closing(self.connect()) as connection:
            return [json.loads(row["payload"]) for row in connection.execute("SELECT payload FROM drafts ORDER BY draft_id")]

    def query(self, scheduler: ScheduleService, request: ScheduleRequest, material_id: str, publish_time: datetime) -> dict:
        with closing(self.connect()) as connection:
            return scheduler.query(connection, request, material_id, publish_time)

    def save(self, request: ScheduleRequest, proposal: DraftProposal, review: ReviewResult,
             queried_times: set[str], guard: PolicyGuard, scheduler: ScheduleService,
             simulate_failure: bool = False) -> dict:
        try:
            with closing(self.connect()) as connection:
                try:
                    # Serialize check + insert across independent processes, not just Python threads.
                    connection.execute("BEGIN IMMEDIATE")
                    existing = self.get(request.fingerprint(), proposal.material_id, connection)
                    if existing:
                        connection.rollback()
                        return {"status": "ALREADY_EXISTS", "draft_id": existing["draft_id"], "draft": existing}
                    slot = scheduler.query(connection, request, proposal.material_id, proposal.suggested_publish_time)
                    draft_status = guard.validate(request, proposal, review, queried_times, slot)
                    now = datetime.now(timezone.utc)
                    draft = ScheduleDraft(
                        draft_id="draft_" + uuid.uuid4().hex, request_fingerprint=request.fingerprint(),
                        material_id=proposal.material_id, original_publish_time=request.requested_publish_time,
                        suggested_publish_time=proposal.suggested_publish_time,
                        target_channel=request.target_channel, market=request.market, timezone=request.timezone,
                        confirmation_status=proposal.confirmation_status, review_status=review.review_status,
                        draft_status=draft_status, risk_reasons=review.risk_reasons,
                        confirmation_evidence=[c for c in request.user_confirmations if c.material_id == proposal.material_id and c.target_time == proposal.suggested_publish_time],
                        created_at=now, updated_at=now,
                    )
                    connection.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?)", (
                        draft.draft_id, request.fingerprint(), draft.material_id, draft.target_channel,
                        draft.market, utc_iso(draft.suggested_publish_time), draft.draft_status, draft.model_dump_json()))
                    if simulate_failure:
                        raise sqlite3.OperationalError("INJECTED_COMMIT_FAILURE")
                    connection.commit()
                    return {"status": "SAVED", "draft_id": draft.draft_id, "draft": draft.model_dump(mode="json")}
                except Exception:
                    connection.rollback()
                    raise
        except PolicyViolation as exc:
            return {"status": "REJECTED_BY_POLICY", "draft_id": None, "error": str(exc)}
        except sqlite3.Error as exc:
            # Deliberately omit SQL / filesystem details from model-facing responses.
            return {"status": "SAVE_FAILED", "draft_id": None, "error": type(exc).__name__,
                    "error_code": "INJECTED_COMMIT_FAILURE" if simulate_failure else "SQLITE_TRANSACTION_FAILED"}
