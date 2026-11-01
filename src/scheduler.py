from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ScheduleRequest, aware


class ScheduleService:
    def __init__(self, window_minutes: int = 60):
        self.window_minutes = window_minutes

    def query(self, connection: sqlite3.Connection, request: ScheduleRequest, material_id: str, publish_time: datetime) -> dict:
        aware(publish_time)
        if abs((publish_time - request.requested_publish_time).total_seconds()) > 7 * 86400:
            raise ValueError("Query must be within seven days of requested time")
        conflicts = []
        fingerprint = request.fingerprint()
        for row in connection.execute("SELECT * FROM formal_schedules WHERE target_channel=? AND market=? AND status='CONFIRMED'", (request.target_channel, request.market)):
            if abs((datetime.fromisoformat(row["publish_time"]) - publish_time).total_seconds()) < self.window_minutes * 60:
                conflicts.append({"source": "formal", "id": row["schedule_id"], "publish_time": row["publish_time"]})
        # Pending suggestions do not reserve globally, but a single request must
        # still propose mutually compatible times for all of its materials.
        for row in connection.execute(
            "SELECT * FROM drafts WHERE target_channel=? AND market=? "
            "AND (draft_status='READY' OR request_fingerprint=?)",
            (request.target_channel, request.market, fingerprint),
        ):
            if row["request_fingerprint"] == fingerprint and row["material_id"] == material_id:
                continue
            if abs((datetime.fromisoformat(row["publish_time"]) - publish_time).total_seconds()) < self.window_minutes * 60:
                conflicts.append({"source": "draft", "id": row["draft_id"], "publish_time": row["publish_time"]})
        window = request.flexible_time_window
        candidates = set()
        if conflicts and window:
            candidates.update((window.start, window.end))
            for conflict in conflicts:
                occupied = datetime.fromisoformat(conflict["publish_time"])
                candidates.update((occupied - timedelta(minutes=self.window_minutes), occupied + timedelta(minutes=self.window_minutes)))
        confirmed = {c.target_time for c in request.user_confirmations if c.material_id == material_id}
        candidates.update(confirmed)
        hints = [{"publish_time": candidate.astimezone(ZoneInfo(request.timezone)).isoformat(),
                  "availability": "UNVERIFIED", "basis": "User-window endpoint, observed conflict spacing boundary, or explicit confirmation"}
                 for candidate in sorted(candidates)
                 if candidate != publish_time and candidate > datetime.now(timezone.utc)
                 and abs((candidate - request.requested_publish_time).total_seconds()) <= 7 * 86400
                 and ((window and window.start <= candidate <= window.end) or candidate in confirmed)]
        return {"available": not conflicts, "publish_time": publish_time.isoformat(), "conflicts": conflicts,
                "conflict_rule": f"Same channel and market; start times less than {self.window_minutes} minutes apart conflict. READY drafts reserve globally; pending suggestions also conflict within this request, but do not reserve for other requests.",
                "search_window": window.model_dump(mode="json") if window else None,
                "query_horizon_days": 7, "minimum_spacing_minutes": self.window_minutes,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "candidate_time_hints": hints,
                "hint": "Exact minimum-spacing boundaries are legal. Candidate hints have NOT been checked for availability: choose and query them yourself. No alternative is implicitly approved."}
