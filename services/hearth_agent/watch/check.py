"""One check of a watched spec: fetch it, diff it against the baseline, record a detection when it breaks.

The first successful check stores the baseline. A later check that changed the spec diffs the two with the
API diff engine; breaking changes become a :class:`Detection` keyed by the Change Record id, so the same
diff seen twice (a re-check, a spec that flips back and forth) never makes a second detection or PR.
The baseline moves to the new spec after every successful diff; a failed fetch or parse keeps the old one.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from apidiff import ApiChange, diff_surfaces, extract_openapi, to_change_record
from apidiff.openapi import parse_spec_text
from services.hearth_agent.models import ChangeKind, PackageRef
from services.hearth_agent.watch.models import Detection, Fix, Watch, WatchCheck
from services.hearth_agent.watch.store import WatchStore

MAX_SPEC_BYTES = 30 * 1024 * 1024
MAX_REDIRECTS = 5


class FetchError(RuntimeError):
    pass


@dataclass
class Fetched:
    text: str | None  # None when the server answered 304 Not Modified
    etag: str | None = None
    last_modified: str | None = None


def _allow_private() -> bool:
    return os.environ.get("HEARTH_WATCH_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


async def _check_public(url: str) -> None:
    """Refuse hosts that resolve to loopback, private or link-local addresses (the Space's own network).

    ``HEARTH_WATCH_ALLOW_PRIVATE=1`` lifts this for specs served inside a private network.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("the spec URL must be http(s)://host/...")
    if _allow_private():
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"cannot resolve {parts.hostname}: {exc}") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise FetchError(f"{parts.hostname} is a private address; set HEARTH_WATCH_ALLOW_PRIVATE=1 to allow it")


async def fetch_spec(url: str, *, etag: str | None = None, last_modified: str | None = None) -> Fetched:
    """GET the spec, conditionally when an ETag / Last-Modified is known. Redirects are re-checked per hop."""
    headers = {"Accept": "application/json, application/yaml, text/yaml, */*;q=0.5",
               "User-Agent": "hearth-api-watch"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_public(url)
            async with client.stream("GET", url, headers=headers) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code == 304:
                    return Fetched(None, etag, last_modified)
                if r.status_code >= 400:
                    raise FetchError(f"GET {url} answered {r.status_code}")
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_SPEC_BYTES:
                        raise FetchError(f"the spec is larger than {MAX_SPEC_BYTES // (1024 * 1024)} MB")
                return Fetched(body.decode(r.encoding or "utf-8", errors="replace"),
                               r.headers.get("etag"), r.headers.get("last-modified"))
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _version(spec: dict[str, Any]) -> str | None:
    info = spec.get("info")
    v = info.get("version") if isinstance(info, dict) else None
    return str(v)[:80] if v not in (None, "") else None


def _change_view(c: ApiChange) -> dict[str, Any]:
    return {**asdict(c), "breaking": c.breaking, "description": c.describe()}


@dataclass
class DiffOutcome:
    old_version: str | None
    new_version: str | None
    changes: list[ApiChange]


def _diff(old_text: str, new_text: str) -> DiffOutcome:
    """CPU-bound (an 8 MB spec takes about a second): run in a worker thread."""
    old, new = parse_spec_text(old_text), parse_spec_text(new_text)
    return DiffOutcome(_version(old), _version(new), diff_surfaces(extract_openapi(old), extract_openapi(new)))


def _validate(text: str) -> str | None:
    """Parse once so a broken first fetch is an error, not a baseline. Returns info.version."""
    spec = parse_spec_text(text)
    extract_openapi(spec)
    return _version(spec)


def _now() -> datetime:
    return datetime.now(UTC)


async def run_check(store: WatchStore, watch: Watch, *, fetch=fetch_spec) -> Detection | None:
    """Check ``watch`` once. Returns a *new* detection when the spec gained breaking changes."""
    status = watch.status
    status.state = "checking"
    store.save()
    now = _now()
    try:
        fetched = await fetch(watch.spec_url, etag=status.etag if status.spec_sha else None,
                              last_modified=status.last_modified if status.spec_sha else None)
        detection = await _apply(store, watch, fetched, now)
    except Exception as exc:  # fetch, parse or diff failure: keep the baseline, report it
        message = str(exc).strip() or type(exc).__name__
        status.state, status.last_error = "error", message[:500]
        watch.record(WatchCheck(at=now, result="error", error=message[:500]))
        detection = None
    else:
        status.state, status.last_error = "idle", None
    status.last_checked_at = now
    status.next_check_at = now + timedelta(minutes=watch.interval_minutes) if watch.enabled else None
    store.save()
    return detection


async def _apply(store: WatchStore, watch: Watch, fetched: Fetched, now: datetime) -> Detection | None:
    status = watch.status
    if watch.id not in store.watches:  # deleted while the spec was downloading
        return None
    if fetched.text is None:
        watch.record(WatchCheck(at=now, result="unchanged", from_version=status.spec_version,
                                to_version=status.spec_version))
        return None
    sha = _sha(fetched.text)
    old_text = store.read_spec(watch.id, status.spec_sha) if status.spec_sha else None
    if sha == status.spec_sha:
        status.etag, status.last_modified = fetched.etag, fetched.last_modified
        watch.record(WatchCheck(at=now, result="unchanged", from_version=status.spec_version,
                                to_version=status.spec_version))
        return None
    if old_text is None:  # first check (or a lost snapshot): this spec becomes the baseline
        version = await asyncio.to_thread(_validate, fetched.text)
        store.write_spec(watch.id, sha, fetched.text)
        status.spec_sha, status.spec_version = sha, version
        status.etag, status.last_modified = fetched.etag, fetched.last_modified
        watch.record(WatchCheck(at=now, result="baseline", to_version=version))
        store.prune_specs(watch.id)
        return None

    outcome = await asyncio.to_thread(_diff, old_text, fetched.text)
    old_sha = status.spec_sha
    from_v = outcome.old_version or old_sha
    to_v = outcome.new_version if outcome.new_version and outcome.new_version != outcome.old_version else sha
    record = to_change_record(
        outcome.changes, provider=watch.name, source_url=watch.spec_url,
        package=PackageRef(ecosystem="openapi", name=watch.name, from_version=from_v, to_version=to_v),
    )
    breaking = sum(c.breaking for c in outcome.changes)
    new_detection: Detection | None = None
    detection_id: str | None = None
    if record is not None and record.kind == ChangeKind.BREAKING:
        detection_id = record.id
        if record.id not in store.detections:
            ordered = sorted(outcome.changes, key=lambda c: not c.breaking)
            new_detection = store.detections[record.id] = Detection(
                id=record.id, watch_id=watch.id, provider=watch.name, detected_at=now,
                from_version=from_v, to_version=to_v, from_sha=old_sha or "", to_sha=sha,
                summary=record.summary, severity=record.severity.value, breaking_count=breaking,
                total_count=len(outcome.changes), change_record=record,
                changes=[_change_view(c) for c in ordered], fix=Fix(updated_at=now),
            )
    store.write_spec(watch.id, sha, fetched.text)
    status.spec_sha, status.spec_version = sha, outcome.new_version
    status.etag, status.last_modified = fetched.etag, fetched.last_modified
    status.last_changed_at = now
    watch.record(WatchCheck(at=now, result="changed", from_version=from_v, to_version=to_v, breaking=breaking,
                            total=len(outcome.changes), detection_id=detection_id))
    store.prune_specs(watch.id)
    return new_detection
