from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .guard import PolicyGuard, PolicyViolation
from .models import DraftProposal, FormalSchedule, ReviewResult, ScheduleDraft, ScheduleRequest, utc_iso
from .rules import DATA_DIR
from .scheduler import ScheduleService


class DraftRepository:
    def __init__(self, path: Path, schedules_path: Path | None = DATA_DIR / "schedules.json"):
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
                CREATE TABLE IF NOT EXISTS draft_revisions (
                  draft_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                  replaced_at TEXT NOT NULL, reason TEXT NOT NULL,
                  PRIMARY KEY(draft_id,revision));
            """)
            # Bootstrap only: subsequent source changes require explicit sync_schedules.
            for item in self._load_schedules(schedules_path) if schedules_path is not None else []:
                connection.execute("INSERT OR IGNORE INTO formal_schedules VALUES(?,?,?,?,?)",
                                   (item.schedule_id, item.target_channel, item.market, utc_iso(item.publish_time), item.status))
            connection.commit()

    @staticmethod
    def _load_schedules(path: Path) -> list[FormalSchedule]:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("Schedule source must be a JSON array")
        records = [FormalSchedule.model_validate(item) for item in data]
        if len({item.schedule_id for item in records}) != len(records):
            raise ValueError("Duplicate schedule_id in schedule source")
        return records

    def sync_schedules(self, path: Path) -> dict:
        # Validate the entire batch before touching any row. Omitted IDs are retained.
        records = self._load_schedules(path)
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        with closing(self.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for item in records:
                    values = (item.target_channel, item.market, utc_iso(item.publish_time), item.status)
                    old = connection.execute("SELECT target_channel,market,publish_time,status FROM formal_schedules WHERE schedule_id=?", (item.schedule_id,)).fetchone()
                    counts["inserted" if old is None else "unchanged" if tuple(old) == values else "updated"] += 1
                    connection.execute("""INSERT INTO formal_schedules VALUES(?,?,?,?,?)
                        ON CONFLICT(schedule_id) DO UPDATE SET target_channel=excluded.target_channel,
                        market=excluded.market,publish_time=excluded.publish_time,status=excluded.status""",
                        (item.schedule_id, *values))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"status": "SYNCHRONIZED", "records": len(records), **counts,
                "omitted_ids": "retained; cancel explicitly with status=CANCELLED"}

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

    def history(self, draft_id: str) -> list[dict]:
        with closing(self.connect()) as connection:
            return [{"revision": row["revision"], "draft": json.loads(row["payload"]),
                     "replaced_at": row["replaced_at"], "reason": row["reason"]}
                    for row in connection.execute("SELECT * FROM draft_revisions WHERE draft_id=? ORDER BY revision", (draft_id,))]

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
                        old_time = datetime.fromisoformat(existing["suggested_publish_time"])
                        old_proposal = DraftProposal(material_id=proposal.material_id, suggested_publish_time=old_time,
                                                     confirmation_status=existing["confirmation_status"])
                        old_slot = scheduler.query(connection, request, proposal.material_id, old_time)
                        try:
                            # Retrieval performs its own fresh query; only new writes require a model query.
                            expected = guard.validate(request, old_proposal, review, {utc_iso(old_time)}, old_slot)
                            if expected != existing["draft_status"]:
                                raise PolicyViolation("STORED_DRAFT_STATUS_MISMATCH")
                        except PolicyViolation as exc:
                            if existing["draft_status"] != "AWAITING_CONFIRMATION" or proposal.suggested_publish_time == old_time:
                                raise PolicyViolation("EXISTING_DRAFT_INVALID: " + str(exc)) from exc
                        else:
                            connection.rollback()
                            return {"status": "ALREADY_EXISTS", "draft_id": existing["draft_id"], "draft": existing,
                                    "schedule_check": old_slot}
                    slot = scheduler.query(connection, request, proposal.material_id, proposal.suggested_publish_time)
                    draft_status = guard.validate(request, proposal, review, queried_times, slot)
                    now = datetime.now(timezone.utc)
                    draft = ScheduleDraft(
                        draft_id=existing["draft_id"] if existing else "draft_" + uuid.uuid4().hex, request_fingerprint=request.fingerprint(),
                        material_id=proposal.material_id, original_publish_time=request.requested_publish_time,
                        suggested_publish_time=proposal.suggested_publish_time,
                        target_channel=request.target_channel, market=request.market, timezone=request.timezone,
                        confirmation_status=proposal.confirmation_status, review_status=review.review_status,
                        draft_status=draft_status, risk_reasons=review.risk_reasons,
                        confirmation_evidence=[c for c in request.user_confirmations if c.material_id == proposal.material_id and c.target_time == proposal.suggested_publish_time],
                        created_at=datetime.fromisoformat(existing["created_at"]) if existing else now, updated_at=now,
                        revision=existing.get("revision", 1) + 1 if existing else 1,
                    )
                    if existing:
                        connection.execute("INSERT INTO draft_revisions VALUES(?,?,?,?,?)", (
                            draft.draft_id, existing.get("revision", 1), json.dumps(existing), now.isoformat(), "STALE_PENDING_REPLAN"))
                        connection.execute("UPDATE drafts SET publish_time=?,draft_status=?,payload=? WHERE draft_id=?", (
                            utc_iso(draft.suggested_publish_time), draft.draft_status, draft.model_dump_json(), draft.draft_id))
                    else:
                        connection.execute("INSERT INTO drafts VALUES(?,?,?,?,?,?,?,?)", (
                            draft.draft_id, request.fingerprint(), draft.material_id, draft.target_channel,
                            draft.market, utc_iso(draft.suggested_publish_time), draft.draft_status, draft.model_dump_json()))
                    if simulate_failure:
                        raise sqlite3.OperationalError("INJECTED_COMMIT_FAILURE")
                    connection.commit()
                    return {"status": "SAVED", "action": "REPLANNED" if existing else "CREATED",
                            "draft_id": draft.draft_id, "draft": draft.model_dump(mode="json"), "schedule_check": slot}
                except Exception:
                    connection.rollback()
                    raise
        except PolicyViolation as exc:
            return {"status": "REJECTED_BY_POLICY", "draft_id": None, "error": str(exc)}
        except sqlite3.Error as exc:
            # Deliberately omit SQL / filesystem details from model-facing responses.
            return {"status": "SAVE_FAILED", "draft_id": None, "error": type(exc).__name__,
                    "error_code": "INJECTED_COMMIT_FAILURE" if simulate_failure else "SQLITE_TRANSACTION_FAILED"}
