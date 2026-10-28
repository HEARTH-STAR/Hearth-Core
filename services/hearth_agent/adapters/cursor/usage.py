"""
Cursor usage — aggregates over the session index (PRD §5: cost per run, monthly budget).

Loaded by ``services.hearth_agent.engine.usage_loader.load_usage_module()`` when ``AGENT_NAME=cursor``.

``cursor-agent`` reports no tokens and no cost on its stream, and its chat store keeps none HEARTH can
read, so the numbers here are what HEARTH itself records per session: runs, steps, status and model. Token
fields are present and zero (``tokens_known: false``) so the usage UI reads cursor the same way as
claude_code; billing lives in the customer's Cursor account.

Same public contract as ``claude_code/usage.py``: ``get_session_files``, ``parse_file``, ``build_summary``,
``dashboard``, ``analytics``, ``summary``, ``summary_card``, ``list_sessions``, ``get_session``,
``aggregate_for_sync`` / ``sync_payload``. ``window`` is ``{"days": N}`` or ``{"start_ms", "end_ms"}``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.hearth_agent.adapters.cursor.paths import iter_transcript
from services.hearth_agent.adapters.cursor.sessions import resolve_native_file
from services.hearth_agent.engine import sessions_io as _session_index

_BACKEND = "cursor"
_TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")


def _empty_tokens() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_KEYS, 0)


def _date(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat() if ms else None


def _window_to_ms(window: dict | None) -> tuple[int | None, int | None]:
    window = window or {}
    if "days" in window:
        days = max(1, min(int(window["days"]), 366))
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        return int(start.timestamp() * 1000), None
    return window.get("start_ms"), window.get("end_ms")


def _rows(window: dict | None = None) -> list[tuple[str, dict]]:
    start_ms, end_ms = _window_to_ms(window)
    out = []
    for key, row in _session_index.iter_session_rows():
        if row.get("backend") != _BACKEND:
            continue
        updated = row.get("updatedAt") or 0
        if (start_ms and updated and updated < start_ms) or (end_ms and updated and updated > end_ms):
            continue
        out.append((key, row))
    return out


# ── Discovery + parser ───────────────────────────────────────────────────────

def get_session_files(*, agent_id: str | None = None) -> list[str]:
    """Transcript paths for indexed cursor sessions (``agent_id`` filters to one key)."""
    files = []
    for key, row in _rows():
        if agent_id and key != agent_id:
            continue
        path = resolve_native_file(row, key)
        if path is not None:
            files.append(str(path))
    return files


def parse_file(path: str, *, start_ms: int | None = None, end_ms: int | None = None) -> tuple[dict, list]:
    """``(meta, entries)``: one entry per user / assistant message. No timestamps or tokens are on file."""
    base = os.path.basename(path)
    meta = {"sessionId": base[: -len(".jsonl")] if base.endswith(".jsonl") else base, "sessionFile": base}
    entries = []
    for record in iter_transcript(Path(path)):
        message = record.get("message")
        role = record.get("role") or (message.get("role") if isinstance(message, dict) else None)
        if role in ("user", "assistant"):
            entries.append({"role": role, "timestamp": None, "usage": _empty_tokens(), "model": "cursor"})
    return meta, entries


# ── Aggregates ───────────────────────────────────────────────────────────────

def build_summary(session_meta: dict, entries: list) -> dict:
    users = sum(1 for e in entries if e["role"] == "user")
    assistants = len(entries) - users
    return {
        "sessionId": session_meta.get("sessionId"),
        "jobId": session_meta.get("jobId"),
        "tokens": _empty_tokens(),
        "tokens_known": False,
        "costUsd": None,
        "numTurns": session_meta.get("numTurns"),
        "messages": {"total": users + assistants, "user": users, "assistant": assistants},
        "firstActivity": session_meta.get("createdAt"),
        "lastActivity": session_meta.get("updatedAt"),
        "avgLatencyMs": None,
        "dailyUsage": [],
        "modelUsage": [],
        "tools": {},
    }


def dashboard(*, window: dict) -> dict:
    """The ``/api/usage`` payload: sessions and steps per day and per model; tokens and cost are unknown."""
    rows = _rows(window)
    by_day: dict[str, dict] = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0, "sessions": 0, "steps": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"model": "", "tokens": 0, "count": 0})
    for _key, row in rows:
        day = _date(row.get("updatedAt"))
        if day:
            by_day[day]["date"] = day
            by_day[day]["sessions"] += 1
            by_day[day]["steps"] += int(row.get("numTurns") or 0)
        model = row.get("model") or "auto"
        by_model[model]["model"] = model
        by_model[model]["count"] += 1
    return {
        "total_sessions": len(rows),
        "total_tokens": _empty_tokens(),
        "tokens_known": False,
        "total_cost": 0.0,
        "by_day": sorted(by_day.values(), key=lambda d: d["date"]),
        "by_model": sorted(by_model.values(), key=lambda m: -m["count"]),
        "top_sessions": [
            {"session_id": k, "job_id": r.get("jobId", k), "status": r.get("status"), "cost": None,
             "tokens": 0, "steps": r.get("numTurns")}
            for k, r in sorted(rows, key=lambda kv: -int(kv[1].get("numTurns") or 0))[:10]
        ],
    }


def analytics(*, window: dict) -> dict:
    d = dashboard(window=window)
    return {"by_day": d["by_day"], "by_model": d["by_model"]}


def summary(*, window: dict) -> dict:
    d = dashboard(window=window)
    return {"total_sessions": d["total_sessions"], "total_tokens": d["total_tokens"], "total_cost": d["total_cost"],
            "tokens_known": False}


def summary_card(*, window: dict) -> dict:
    s = summary(window=window)
    return {"sessions": s["total_sessions"], "tokens": 0, "cost": 0.0, "tokens_known": False}


def list_sessions() -> dict:
    return {"sessions": [
        {"session_id": k, "job_id": r.get("jobId", k), "native_session_id": r.get("nativeSessionId"),
         "status": r.get("status"), "model": r.get("model"), "cost": None, "tokens": _empty_tokens(),
         "steps": r.get("numTurns"), "updated_at": r.get("updatedAt")}
        for k, r in _rows()
    ]}


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    row = _session_index.read_session_row(session_id)
    if row is None or row.get("backend") != _BACKEND:
        return None
    path = resolve_native_file(row, session_id)
    entries = parse_file(str(path))[1] if path else []
    return build_summary({"sessionId": session_id, "jobId": row.get("jobId", session_id),
                          "numTurns": row.get("numTurns"), "createdAt": row.get("createdAt"),
                          "updatedAt": row.get("updatedAt")}, entries)


def aggregate_for_sync(*, since_date: str | None = None) -> dict:
    """Per-day totals with no identifiers — the only shape usage may take outside the space (PRD §4)."""
    days: dict[str, dict] = defaultdict(lambda: {"date": "", "sessions": 0, "tokens": 0, "cost": 0.0})
    for _key, row in _rows():
        day = _date(row.get("updatedAt"))
        if not day or (since_date and day < since_date):
            continue
        days[day]["date"] = day
        days[day]["sessions"] += 1
    return {"days": sorted(days.values(), key=lambda d: d["date"])}


sync_payload = aggregate_for_sync

__all__: list[str] = [
    "aggregate_for_sync", "analytics", "build_summary", "dashboard", "get_session", "get_session_files",
    "list_sessions", "parse_file", "summary", "summary_card", "sync_payload",
]
