"""
ies_store.py
------------
Disk persistence for uploaded/fetched IES files.

Why this exists: cPanel/Passenger recycles the app's process periodically
(idle timeout, restarts, deploys), and the original design kept parsed IES
data only in an in-memory dict — every recycle silently lost every file
the user had uploaded. This module puts the raw file bytes on disk so a
save survives a restart and can be reused without re-uploading.

Layout on disk, under config.IES_STORAGE_DIR (created on first use):
    <id>.ies    - the raw IES text, exactly as received
    <id>.json   - sidecar metadata: filename, saved_at, source, source_url

This module only handles bytes-on-disk and listing what's there. The
*parsed* IesData objects used mid-calculation still live in the
in-memory `_ies_store` dict in api_routes.py (re-parsing on every request
would be wasteful) - api_routes.py re-populates that dict from disk on
import, so a fresh process picks up everything already saved.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from config import IES_STORAGE_DIR

STORAGE_DIR = Path(IES_STORAGE_DIR)


def _ies_path(ies_id: str) -> Path:
    return STORAGE_DIR / f"{ies_id}.ies"


def _meta_path(ies_id: str) -> Path:
    return STORAGE_DIR / f"{ies_id}.json"


def save(raw_text: str, filename: str, source: str = "upload",
         source_url: Optional[str] = None) -> str:
    """Persist a raw IES text blob to disk and return its new id."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    ies_id = str(uuid.uuid4())
    _ies_path(ies_id).write_text(raw_text, encoding="utf-8")
    meta = {
        "filename": filename,
        "saved_at": time.time(),
        "source": source,          # "upload" | "url"
        "source_url": source_url,  # only set when source == "url"
    }
    _meta_path(ies_id).write_text(json.dumps(meta), encoding="utf-8")
    return ies_id


def load_text(ies_id: str) -> Optional[str]:
    path = _ies_path(ies_id)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_meta(ies_id: str) -> Optional[dict]:
    path = _meta_path(ies_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete(ies_id: str) -> bool:
    """Remove a saved file (both the .ies and .json). Returns True if
    anything was actually deleted."""
    found = False
    for path in (_ies_path(ies_id), _meta_path(ies_id)):
        if path.exists():
            path.unlink()
            found = True
    return found


def list_saved() -> list[dict]:
    """Return [{id, filename, saved_at, source, source_url}, ...] for
    every file on disk, most recently saved first."""
    if not STORAGE_DIR.exists():
        return []
    items = []
    for meta_file in STORAGE_DIR.glob("*.json"):
        ies_id = meta_file.stem
        meta = load_meta(ies_id)
        if meta is None:
            continue
        items.append({"id": ies_id, **meta})
    items.sort(key=lambda m: m.get("saved_at", 0), reverse=True)
    return items
