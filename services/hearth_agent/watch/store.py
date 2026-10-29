"""The API watch's state on the space volume.

    $HEARTH_STATE_DIR/watch/
      state.json                 watches and detections (``{"watches": {...}, "detections": {...}}``)
      specs/<watch_id>/<sha>.txt spec snapshots: the baseline and every version a detection refers to

The store is loaded once per state root and mutated only from the event loop, without awaiting between a
read and its write, so no lock is needed; every write is atomic (temp file + ``os.replace``).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from services.hearth_agent.watch.models import Detection, Watch
from services.storage.paths import hearth_state_dir

log = logging.getLogger(__name__)

_stores: dict[Path, WatchStore] = {}


def watch_dir() -> Path:
    return hearth_state_dir() / "watch"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class WatchStore:
    def __init__(self, root: Path):
        self.root = root
        self.watches: dict[str, Watch] = {}
        self.detections: dict[str, Detection] = {}
        self._load()

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    def _load(self) -> None:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("unreadable %s, starting empty: %s", self.state_file, exc)
            return
        for key, value in (raw.get("watches") or {}).items():
            try:
                self.watches[key] = Watch.model_validate(value)
            except ValueError as exc:
                log.error("dropping unreadable watch %s: %s", key, exc)
        for key, value in (raw.get("detections") or {}).items():
            try:
                self.detections[key] = Detection.model_validate(value)
            except ValueError as exc:
                log.error("dropping unreadable detection %s: %s", key, exc)

    def save(self) -> None:
        data = {
            "watches": {k: w.model_dump(mode="json") for k, w in self.watches.items()},
            "detections": {k: d.model_dump(mode="json", by_alias=True) for k, d in self.detections.items()},
        }
        _atomic_write(self.state_file, json.dumps(data, indent=1))

    # -- spec snapshots --------------------------------------------------------

    def spec_path(self, watch_id: str, sha: str) -> Path:
        return self.root / "specs" / watch_id / f"{sha}.txt"

    def write_spec(self, watch_id: str, sha: str, text: str) -> None:
        path = self.spec_path(watch_id, sha)
        if not path.exists():
            _atomic_write(path, text)

    def read_spec(self, watch_id: str, sha: str) -> str | None:
        try:
            return self.spec_path(watch_id, sha).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def prune_specs(self, watch_id: str) -> None:
        """Drop snapshots that are neither the baseline nor referenced by a detection."""
        watch = self.watches.get(watch_id)
        keep = {watch.status.spec_sha} if watch else set()
        for d in self.detections.values():
            if d.watch_id == watch_id:
                keep |= {d.from_sha, d.to_sha}
        folder = self.root / "specs" / watch_id
        for f in folder.glob("*.txt") if folder.is_dir() else ():
            if f.stem not in keep:
                f.unlink(missing_ok=True)

    def remove_watch(self, watch_id: str) -> None:
        self.watches.pop(watch_id, None)
        for key in [k for k, d in self.detections.items() if d.watch_id == watch_id]:
            del self.detections[key]
        folder = self.root / "specs" / watch_id
        for f in folder.glob("*.txt") if folder.is_dir() else ():
            f.unlink(missing_ok=True)
        if folder.is_dir():
            folder.rmdir()
        self.save()


def get_store() -> WatchStore:
    """The store for the current ``HEARTH_STATE_DIR`` (tests point it at a temp dir per test)."""
    root = watch_dir()
    store = _stores.get(root)
    if store is None:
        store = _stores[root] = WatchStore(root)
    return store
