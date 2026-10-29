"""
Antigravity usage — aggregates over the session index (PRD §5: cost per run, monthly budget).

Loaded by ``engine.usage_loader.load_usage_module()`` when ``AGENT_NAME=antigravity``. Each row carries the
conversation's tokens as agy recorded them (``tokens.py``: its own SQLite store, rolled up by the adapter).
They are agy's **client-side estimates**, with no cached-token split and no hidden reasoning tokens, so
they are reported with ``tokens_estimated: true`` and no cost (billing is the Google account's).

Same public contract as ``claude_code/usage.py``: ``get_session_files``, ``parse_file``, ``build_summary``,
``dashboard``, ``analytics``, ``summary``, ``summary_card``, ``list_sessions``, ``get_session``,
``aggregate_for_sync`` / ``sync_payload``. ``window`` is ``{"days": N}`` or ``{"start_ms", "end_ms"}``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from services.hearth_agent.adapters.antigravity import transcript as _t
from services.hearth_agent.adapters.antigravity.sessions import resolve_native_file
from services.hearth_agent.adapters.antigravity.tokens import conversation_tokens
from services.hearth_agent.engine import sessions_io as _session_index

_BACKEND = "antigravity"


def _tokens(row: dict) -> dict[str, int]:
    u = row.get("usage") or {}
    t = {"input": int(u.get("input_tokens", 0)), "output": int(u.get("output_tokens", 0)), "cacheRead": 0,
         "cacheWrite": 0}
    t["totalTokens"] = t["input"] + t["output"]
    return t


def _empty_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0}


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


def get_session_files(*, agent_id: str | None = None) -> list[str]:
    files = []
    for key, row in _rows():
        if agent_id and key != agent_id:
            continue
        path = resolve_native_file(row, key)
        if path is not None:
            files.append(str(path))
    return files


def parse_file(path: str, *, start_ms: int | None = None, end_ms: int | None = None) -> tuple[dict, list]:
    """``(meta, entries)`` from a transcript: one entry per turn; tokens are on the summary only."""
    conversation = path.replace("\\", "/").split("/brain/", 1)[-1].split("/", 1)[0]
    entries = []
    for turn in _t.iter_turns(_t.read_steps(conversation)):
        ts = turn.get("ts_ms")
        if ts and ((start_ms and ts < start_ms) or (end_ms and ts > end_ms)):
            continue
        entries.append({"role": turn["role"], "timestamp": ts, "toolNames": turn.get("tool_names") or []})
    return {"sessionId": conversation, "sessionFile": path}, entries


def build_summary(session_meta: dict, entries: list) -> dict:
    users = sum(1 for e in entries if e["role"] == "user")
    tools: dict[str, int] = defaultdict(int)
    for e in entries:
        for name in e.get("toolNames") or []:
            tools[name] += 1
    stamps = [e["timestamp"] for e in entries if e.get("timestamp")]
    return {
        "sessionId": session_meta.get("sessionId"),
        "jobId": session_meta.get("jobId"),
        "tokens": session_meta.get("tokens") or _empty_tokens(),
        "tokens_estimated": True,
        "costUsd": None,
        "numTurns": session_meta.get("numTurns"),
        "messages": {"total": len(entries), "user": users, "assistant": len(entries) - users},
        "firstActivity": min(stamps) if stamps else None,
        "lastActivity": max(stamps) if stamps else None,
        "avgLatencyMs": None,
        "dailyUsage": [],
        "modelUsage": [],
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
    }


def dashboard(*, window: dict) -> dict:
    rows = _rows(window)
    totals = _empty_tokens()
    by_day: dict[str, dict] = defaultdict(lambda: {"date": "", "tokens": 0, "cost": 0.0, "sessions": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"model": "", "tokens": 0, "count": 0})
    for _key, row in rows:
        tokens = _tokens(row)
        for k in totals:
            totals[k] += tokens[k]
        day = _date(row.get("updatedAt"))
        if day:
            by_day[day]["date"] = day
            by_day[day]["tokens"] += tokens["totalTokens"]
            by_day[day]["sessions"] += 1
        model = row.get("model") or "unknown"
        by_model[model]["model"] = model
        by_model[model]["tokens"] += tokens["totalTokens"]
        by_model[model]["count"] += 1
    return {
        "total_sessions": len(rows),
        "total_tokens": totals,
        "tokens_estimated": True,
        "total_cost": 0.0,
        "by_day": sorted(by_day.values(), key=lambda d: d["date"]),
        "by_model": sorted(by_model.values(), key=lambda m: -m["tokens"]),
        "top_sessions": sorted(
            ({"session_id": k, "job_id": r.get("jobId", k), "status": r.get("status"), "cost": None,
              "tokens": _tokens(r)["totalTokens"]} for k, r in rows),
            key=lambda s: -s["tokens"],
        )[:10],
    }


def analytics(*, window: dict) -> dict:
    d = dashboard(window=window)
    return {"by_day": d["by_day"], "by_model": d["by_model"]}


def summary(*, window: dict) -> dict:
    d = dashboard(window=window)
    return {"total_sessions": d["total_sessions"], "total_tokens": d["total_tokens"], "total_cost": 0.0,
            "tokens_estimated": True}


def summary_card(*, window: dict) -> dict:
    s = summary(window=window)
    return {"sessions": s["total_sessions"], "tokens": s["total_tokens"]["totalTokens"], "cost": 0.0,
            "tokens_estimated": True}


def list_sessions() -> dict:
    return {"sessions": [
        {"session_id": k, "job_id": r.get("jobId", k), "native_session_id": r.get("nativeSessionId"),
         "status": r.get("status"), "model": r.get("model"), "cost": None, "tokens": _tokens(r),
         "updated_at": r.get("updatedAt")}
        for k, r in _rows()
    ]}


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    row = _session_index.read_session_row(session_id)
    if row is None or row.get("backend") != _BACKEND:
        return None
    start_ms, end_ms = _window_to_ms(window)
    path = resolve_native_file(row, session_id)
    entries = parse_file(str(path), start_ms=start_ms, end_ms=end_ms)[1] if path else []
    tokens = _tokens(row)
    native = row.get("nativeSessionId") or ""
    if native and not tokens["totalTokens"]:  # a run still going: read agy's store directly
        live = conversation_tokens(native)
        tokens = {**tokens, "input": live["total_input"], "output": live["total_output"],
                  "totalTokens": live["total_input"] + live["total_output"]}
    return build_summary({"sessionId": session_id, "jobId": row.get("jobId", session_id),
                          "numTurns": row.get("numTurns"), "tokens": tokens}, entries)


def aggregate_for_sync(*, since_date: str | None = None) -> dict:
    """Per-day totals with no identifiers — the only shape usage may take outside the space (PRD §4)."""
    days: dict[str, dict] = defaultdict(lambda: {"date": "", "sessions": 0, "tokens": 0, "cost": 0.0})
    for _key, row in _rows():
        day = _date(row.get("updatedAt"))
        if not day or (since_date and day < since_date):
            continue
        days[day]["date"] = day
        days[day]["sessions"] += 1
        days[day]["tokens"] += _tokens(row)["totalTokens"]
    return {"days": sorted(days.values(), key=lambda d: d["date"])}


sync_payload = aggregate_for_sync

__all__: list[str] = [
    "aggregate_for_sync", "analytics", "build_summary", "dashboard", "get_session", "get_session_files",
    "list_sessions", "parse_file", "summary", "summary_card", "sync_payload",
]
