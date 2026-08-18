#!/usr/bin/env python3
"""Loopback-only, read-only adapter for the LMI lead pipeline.

The OpenComputer shadow agent must not open the production SQLite database or
reuse the ChatGPT browser profile.  This process owns that boundary: it opens
SQLite in immutable read-only mode, executes only fixed SELECT templates, and
returns a deliberately small JSON projection over loopback HTTP.
"""

from __future__ import annotations

import json
import os
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


DEFAULT_DB = "/root/unipile_webhooks.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18650
MAX_LIMIT = 100
LEAD_COLUMNS = (
    "member_id",
    "first_name",
    "last_name",
    "company",
    "headline",
    "public_profile_url",
    "icp_tier",
    "icp_score",
    "stage",
    "outreach_status",
    "next_channel",
    "replied",
    "meeting_booked",
    "last_reply_at",
    "enriched_at",
    "source",
)
ALLOWED_STATUSES = frozenset(
    {"all", "qualified", "interested", "in_conversation", "engaged", "new"}
)


class ReaderError(RuntimeError):
    pass


class LMIShadowReader:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise ReaderError("lead database is unavailable")
        con = sqlite3.connect(
            f"file:{self.db_path}?mode=ro&immutable=1",
            uri=True,
            timeout=5,
        )
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        if table not in {"leads", "research_import_receipts", "research_runs"}:
            raise ReaderError("unsupported table")
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}

    def health(self) -> dict[str, object]:
        try:
            with self._connect() as con:
                count = int(con.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
            return {"ok": True, "service": "lmi-shadow-reader", "lead_count": count}
        except (sqlite3.Error, ReaderError):
            return {"ok": False, "service": "lmi-shadow-reader"}

    def leads(self, *, limit: int, status: str) -> dict[str, object]:
        limit = max(1, min(int(limit), MAX_LIMIT))
        status = status.strip().lower()
        if status not in ALLOWED_STATUSES:
            raise ReaderError("unsupported status filter")

        with self._connect() as con:
            columns = self._columns(con, "leads")
            selected = [name for name in LEAD_COLUMNS if name in columns]
            if "member_id" not in selected:
                raise ReaderError("lead schema is missing member_id")

            predicates: list[str] = []
            params: list[object] = []
            if "dup_of" in columns:
                predicates.append("dup_of IS NULL")
            if status != "all":
                available_status_columns = [
                    name for name in ("outreach_status", "stage") if name in columns
                ]
                if not available_status_columns:
                    raise ReaderError("lead schema has no status column")
                predicates.append(
                    "(" + " OR ".join(f"{name}=?" for name in available_status_columns) + ")"
                )
                params.extend([status] * len(available_status_columns))

            order = "id DESC" if "id" in columns else "rowid DESC"
            where = " WHERE " + " AND ".join(predicates) if predicates else ""
            sql = f"SELECT {', '.join(selected)} FROM leads{where} ORDER BY {order} LIMIT ?"
            params.append(limit)
            rows = [dict(row) for row in con.execute(sql, params)]

        return {"ok": True, "count": len(rows), "status": status, "leads": rows}

    def summary(self) -> dict[str, object]:
        out: dict[str, object] = {"ok": True, "service": "lmi-shadow-reader"}
        with self._connect() as con:
            columns = self._columns(con, "leads")
            out["lead_count"] = int(con.execute("SELECT COUNT(*) FROM leads").fetchone()[0])
            for name in ("stage", "outreach_status", "icp_tier", "next_channel"):
                if name not in columns:
                    continue
                rows = con.execute(
                    f"SELECT COALESCE({name}, 'unknown') value, COUNT(*) count "
                    f"FROM leads GROUP BY {name} ORDER BY count DESC LIMIT 50"
                )
                out[name] = {str(row["value"]): int(row["count"]) for row in rows}
        return out


class Handler(BaseHTTPRequestHandler):
    reader: LMIShadowReader

    def log_message(self, fmt: str, *args: object) -> None:
        # Paths/status only; never log response bodies or lead content.
        super().log_message(fmt, *args)

    def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                payload = self.reader.health()
                status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
                self._json(status, payload)
                return
            if parsed.path == "/v1/pipeline-summary":
                self._json(HTTPStatus.OK, self.reader.summary())
                return
            if parsed.path == "/v1/leads":
                query = parse_qs(parsed.query)
                raw_limit = query.get("limit", ["25"])[0]
                status_filter = query.get("status", ["qualified"])[0]
                try:
                    limit = int(raw_limit)
                except ValueError as exc:
                    raise ReaderError("limit must be an integer") from exc
                self._json(
                    HTTPStatus.OK,
                    self.reader.leads(limit=limit, status=status_filter),
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
        except (ReaderError, sqlite3.Error) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})


def main() -> None:
    db_path = os.environ.get("LMI_SHADOW_DB", DEFAULT_DB)
    host = os.environ.get("LMI_SHADOW_HOST", DEFAULT_HOST)
    port = int(os.environ.get("LMI_SHADOW_PORT", str(DEFAULT_PORT)))
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("lmi-shadow-reader must bind to loopback")
    Handler.reader = LMIShadowReader(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
