from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .models import ScheduleRequest, aware


class ScheduleService:
    def __init__(self, window_minutes: int = 60):
        self.window_minutes = window_minutes

    def query(self, connection: sqlite3.Connection, request: ScheduleRequest, material_id: str, publish_time: datetime) -> dict:
        aware(publish_time)
        if abs((publish_time - request.requested_publish_time).total_seconds()) > 7 * 86400:
            raise ValueError("Query must be within seven days of requested time")
        conflicts = []
        for row in connection.execute("SELECT * FROM formal_schedules WHERE target_channel=? AND market=? AND status='CONFIRMED'", (request.target_channel, request.market)):
            if abs((datetime.fromisoformat(row["publish_time"]) - publish_time).total_seconds()) < self.window_minutes * 60:
                conflicts.append({"source": "formal", "id": row["schedule_id"], "publish_time": row["publish_time"]})
        for row in connection.execute("SELECT * FROM drafts WHERE target_channel=? AND market=? AND draft_status='READY'", (request.target_channel, request.market)):
            if row["request_fingerprint"] == request.fingerprint() and row["material_id"] == material_id:
                continue
            if abs((datetime.fromisoformat(row["publish_time"]) - publish_time).total_seconds()) < self.window_minutes * 60:
                conflicts.append({"source": "draft", "id": row["draft_id"], "publish_time": row["publish_time"]})
        window = request.flexible_time_window
        return {"available": not conflicts, "publish_time": publish_time.isoformat(), "conflicts": conflicts,
                "conflict_rule": f"Same channel and market; start times less than {self.window_minutes} minutes apart conflict. READY drafts reserve; pending suggestions do not.",
                "search_window": window.model_dump(mode="json") if window else None,
                "query_horizon_days": 7, "minimum_spacing_minutes": self.window_minutes,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "hint": "Choose and query another time if needed; no alternative is implicitly approved."}
