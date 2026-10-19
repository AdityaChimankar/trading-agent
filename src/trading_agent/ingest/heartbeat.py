"""Persist ticker health so the API and scheduler see the same feed state."""
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from trading_agent.core.config import get_settings


def write_heartbeat(connected: bool, last_tick: datetime | None, buffer_depth: int = 0) -> None:
    path = get_settings().data_dir / "ticker-heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"connected": connected, "last_tick": last_tick.isoformat() if last_tick else None,
                                        "updated_at": datetime.now(UTC).isoformat(), "buffer_depth": buffer_depth}), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_heartbeat() -> dict | None:
    path = get_settings().data_dir / "ticker-heartbeat.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        age = (datetime.now(UTC) - datetime.fromisoformat(result["updated_at"])).total_seconds()
        result["heartbeat_age_sec"] = max(0, age)
        result["connected"] = bool(result["connected"] and 0 <= age < 90)
        result["repairs_queued"] = 0
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None
