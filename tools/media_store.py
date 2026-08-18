"""Durable generated-media store.

Videos and images used to live only as loose files under HERMES_HOME
(plus ephemeral provider CDNs). This module keeps the bytes in
``$HERMES_HOME/media.db`` so they survive cache cleanup and are queryable
alongside the session DB.

``state.db`` stays text/session-only — blobs here would bloat FTS and
session queries. ``media.db`` is the source of truth; cache files remain
a playback convenience and can be rebuilt with :func:`export_media`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_NAME = "media.db"
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_INGEST_DIRS = (
    "video_gen",
    "image_gen",
    "images",
    "cache/images",
    "cache/videos",
    "image_cache",
)
_INGEST_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
}

_KIND_BY_SUFFIX = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".mkv": "video",
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS generated_media (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT,
    sha256 TEXT NOT NULL UNIQUE,
    bytes BLOB NOT NULL,
    byte_size INTEGER NOT NULL,
    source_path TEXT,
    source_url TEXT,
    created_at REAL NOT NULL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_generated_media_kind ON generated_media(kind);
CREATE INDEX IF NOT EXISTS idx_generated_media_created ON generated_media(created_at DESC);
"""


def media_db_path() -> Path:
    return get_hermes_home() / _DB_NAME


def _connect() -> sqlite3.Connection:
    path = media_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def _kind_from_name(filename: str, default: str = "file") -> str:
    return _KIND_BY_SUFFIX.get(Path(filename).suffix.lower(), default)


def _row_to_record(row: sqlite3.Row, *, include_bytes: bool = False) -> dict[str, Any]:
    record = {
        "id": row["id"],
        "kind": row["kind"],
        "filename": row["filename"],
        "mime": row["mime"],
        "sha256": row["sha256"],
        "byte_size": row["byte_size"],
        "source_path": row["source_path"],
        "source_url": row["source_url"],
        "created_at": row["created_at"],
        "meta": json.loads(row["meta"]) if row["meta"] else {},
        "media_db": str(media_db_path()),
    }
    if include_bytes:
        record["bytes"] = bytes(row["bytes"])
    return record


def put_bytes(
    data: bytes,
    *,
    kind: str,
    filename: str,
    source_path: Optional[str] = None,
    source_url: Optional[str] = None,
    mime: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Insert or reuse a media row. Idempotent on content hash."""
    if not data:
        raise ValueError("refusing to store empty media")
    digest = hashlib.sha256(data).hexdigest()
    filename = Path(filename or "media.bin").name
    mime = mime or _guess_mime(filename)
    payload_meta = json.dumps(meta or {}, ensure_ascii=False)
    now = time.time()
    media_id = f"med_{uuid.uuid4().hex[:16]}"

    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM generated_media WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if existing:
            record = _row_to_record(existing)
            record["deduped"] = True
            return record

        conn.execute(
            """
            INSERT INTO generated_media (
                id, kind, filename, mime, sha256, bytes, byte_size,
                source_path, source_url, created_at, meta
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                kind,
                filename,
                mime,
                digest,
                sqlite3.Binary(data),
                len(data),
                source_path,
                source_url,
                now,
                payload_meta,
            ),
        )
        row = conn.execute(
            "SELECT * FROM generated_media WHERE id = ?",
            (media_id,),
        ).fetchone()
    return _row_to_record(row)


def put_file(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    file_path = Path(path).expanduser().resolve()
    data = file_path.read_bytes()
    kind = kwargs.pop("kind", None) or _kind_from_name(file_path.name)
    filename = kwargs.pop("filename", None) or file_path.name
    return put_bytes(
        data,
        kind=kind,
        filename=filename,
        source_path=str(file_path),
        **kwargs,
    )


def put_url(url: str, **kwargs: Any) -> dict[str, Any]:
    """Download a public URL and store the bytes."""
    import urllib.request

    parsed = urlparse(url)
    if parsed.scheme == "file":
        return put_file(parsed.path, **kwargs)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported media URL scheme: {parsed.scheme!r}")

    request = urllib.request.Request(url, headers={"User-Agent": "OpenComputer-media-store/1"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - http/https only
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 64)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise ValueError(f"media URL exceeded {_MAX_DOWNLOAD_BYTES} bytes")
            chunks.append(chunk)
    data = b"".join(chunks)
    name = Path(parsed.path).name or kwargs.pop("filename", None) or "download.bin"
    kind = kwargs.pop("kind", None) or _kind_from_name(name, default="file")
    mime = kwargs.pop("mime", None) or content_type or _guess_mime(name)
    return put_bytes(
        data,
        kind=kind,
        filename=name,
        source_url=url,
        mime=mime,
        **kwargs,
    )


def get_media(media_id: str, *, include_bytes: bool = False) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM generated_media WHERE id = ?",
            (media_id,),
        ).fetchone()
    return _row_to_record(row, include_bytes=include_bytes) if row else None


def get_by_sha256(digest: str, *, include_bytes: bool = False) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM generated_media WHERE sha256 = ?",
            (digest,),
        ).fetchone()
    return _row_to_record(row, include_bytes=include_bytes) if row else None


def list_media(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM generated_media ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def export_media(media_id: str, dest: str | Path | None = None) -> Path:
    record = get_media(media_id, include_bytes=True)
    if record is None:
        raise FileNotFoundError(f"media id not found: {media_id}")
    if dest is None:
        dest = get_hermes_home() / "media_export" / record["filename"]
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(record["bytes"])
    return dest_path


def ingest_paths(paths: Iterable[str | Path], *, meta: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file() or path.suffix.lower() not in _INGEST_SUFFIXES:
            continue
        try:
            stored.append(put_file(path, meta=meta))
        except Exception as exc:  # noqa: BLE001 - ingest should skip bad files
            logger.warning("Could not ingest %s: %s", path, exc)
    return stored


def ingest_existing(home: Optional[Path] = None) -> dict[str, Any]:
    """Copy known generated/uploaded media dirs into media.db."""
    root = Path(home) if home is not None else get_hermes_home()
    found: list[Path] = []
    for rel in _INGEST_DIRS:
        directory = root / rel
        if not directory.is_dir():
            continue
        found.extend(
            p for p in directory.iterdir()
            if p.is_file() and p.suffix.lower() in _INGEST_SUFFIXES
        )
    records = ingest_paths(found, meta={"ingested_from": "existing_files"})
    return {
        "media_db": str(media_db_path()),
        "scanned": len(found),
        "stored": len(records),
        "bytes": sum(r["byte_size"] for r in records),
        "ids": [r["id"] for r in records],
        "files": [r["filename"] for r in records],
    }


def persist_generated_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Store image/video bytes from a successful generate result.

    Never raises — generation success must not depend on the store.
    """
    if not isinstance(payload, dict) or not payload.get("success"):
        return payload
    try:
        record = _persist_from_payload(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist generated media to media.db: %s", exc)
        payload.setdefault("media_store_error", str(exc))
        return payload
    if record:
        payload["media_id"] = record["id"]
        payload["media_db"] = record["media_db"]
        payload["media_sha256"] = record["sha256"]
        payload["media_bytes"] = record["byte_size"]
        payload["media_filename"] = record["filename"]
    return payload


def _local_file_candidate(value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return None
    path = Path(value)
    if path.is_file():
        return path
    return None


def _url_candidate(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith(("http://", "https://", "file://")):
        return value
    return None


def _persist_from_payload(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    kind = "video" if payload.get("video") else "image"
    meta = {
        "model": payload.get("model"),
        "provider": payload.get("provider"),
        "prompt": payload.get("prompt"),
    }
    for key in ("image", "video", "host_image"):
        path = _local_file_candidate(payload.get(key))
        if path is not None:
            return put_file(path, kind=kind, meta=meta)
    last_error: Exception | None = None
    for key in ("image", "video", "public_url", "temporary_url"):
        url = _url_candidate(payload.get(key))
        if not url:
            continue
        try:
            return put_url(url, kind=kind, meta=meta)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            last_error = exc
            logger.info("media URL persist skipped %s: %s", url, exc)
    if last_error is not None:
        raise last_error
    return None
