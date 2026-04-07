# coding=utf-8
"""
DAY VIBE AI 的本地存储。

使用单独的 SQLite 数据库存储日报、收藏和阅读日志，便于后续迁移到 Postgres。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BookmarkRecord:
    item_id: str
    report_id: str
    title: str
    created_at: str
    note: str = ""


def _default_data_dir() -> Path:
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
        return Path(os.environ.get("DAY_VIBE_DATA_DIR", "/tmp/day-vibe-ai/assistant"))
    return Path(os.environ.get("DAY_VIBE_DATA_DIR", "output/assistant"))


class AssistantStorage:
    """DAY VIBE AI 的 SQLite 存储。"""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "assistant.db"
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_id TEXT PRIMARY KEY,
            report_date TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            title TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_report_items (
            item_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL,
            item_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            original_title TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_url TEXT NOT NULL,
            image_url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            importance REAL NOT NULL DEFAULT 0,
            importance_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(report_id) REFERENCES daily_reports(report_id)
        );

        CREATE TABLE IF NOT EXISTS bookmarks (
            item_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reading_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            report_id TEXT NOT NULL,
            draft_title TEXT NOT NULL DEFAULT '',
            log_title TEXT NOT NULL DEFAULT '',
            log_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_daily_reports_date ON daily_reports(report_date DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_items_report ON daily_report_items(report_id, item_index);
        CREATE INDEX IF NOT EXISTS idx_bookmarks_created ON bookmarks(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_logs_item ON reading_logs(item_id, created_at DESC);
        """

        with self._connect() as conn:
            conn.executescript(schema)
            self._ensure_column(conn, "reading_logs", "draft_text", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "reading_logs", "draft_title", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "reading_logs", "log_title", "TEXT NOT NULL DEFAULT ''")
            conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row[1] == column for row in rows):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def save_report(self, report: Dict[str, Any]) -> None:
        """保存日报以及其条目。"""
        now_str = self._now()
        report_id = report["report_id"]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_reports
                (report_id, report_date, generated_at, title, window_start, window_end, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    report["report_date"],
                    report["generated_at"],
                    report["title"],
                    report["window_start"],
                    report["window_end"],
                    json.dumps(report, ensure_ascii=False),
                ),
            )

            conn.execute("DELETE FROM daily_report_items WHERE report_id = ?", (report_id,))
            for index, item in enumerate(report.get("items", []), start=1):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO daily_report_items
                    (item_id, report_id, item_index, title, original_title, summary,
                     source_name, source_type, source_url, image_url, published_at,
                     importance, importance_reason, created_at, updated_at, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["item_id"],
                        report_id,
                        index,
                        item["title"],
                        item.get("original_title", item["title"]),
                        item["summary"],
                        item["source_name"],
                        item.get("source_type", ""),
                        item.get("source_url", ""),
                        item.get("image_url", ""),
                        item.get("published_at", ""),
                        item.get("importance", 0),
                        item.get("importance_reason", ""),
                        now_str,
                        now_str,
                        json.dumps(item, ensure_ascii=False),
                    ),
                )

            conn.commit()

    def list_reports(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT report_id, report_date, generated_at, title, window_start, window_end, payload_json
                FROM daily_reports
                ORDER BY generated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_latest_report(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM daily_reports
                ORDER BY generated_at DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM daily_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])

    def list_report_items(self, report_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_report_items
                WHERE report_id = ?
                ORDER BY item_index ASC
                """,
                (report_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_report_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_report_items WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_bookmark(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT b.*, i.summary, i.source_name, i.source_url, i.image_url, i.importance
                FROM bookmarks b
                LEFT JOIN daily_report_items i ON i.item_id = b.item_id
                WHERE b.item_id = ?
                """,
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_bookmark(self, item: Dict[str, Any], note: str = "") -> Dict[str, Any]:
        now_str = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT item_id FROM bookmarks WHERE item_id = ?",
                (item["item_id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE bookmarks
                    SET report_id = ?, title = ?, note = ?, updated_at = ?
                    WHERE item_id = ?
                    """,
                    (
                        item["report_id"],
                        item["title"],
                        note,
                        now_str,
                        item["item_id"],
                    ),
                )
                conn.commit()
                return {"bookmarked": True}

            conn.execute(
                """
                INSERT INTO bookmarks (item_id, report_id, title, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item["item_id"],
                    item["report_id"],
                    item["title"],
                    note,
                    now_str,
                    now_str,
                ),
            )
            conn.commit()
            return {"bookmarked": True}

    def remove_bookmark(self, item_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("DELETE FROM bookmarks WHERE item_id = ?", (item_id,))
            conn.commit()
        return {"bookmarked": False}

    def list_bookmarks(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT b.*, i.summary, i.source_name, i.source_url, i.image_url, i.importance
                FROM bookmarks b
                LEFT JOIN daily_report_items i ON i.item_id = b.item_id
                ORDER BY b.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def add_reading_log(self, item: Dict[str, Any], log_text: str, log_title: str = "") -> Dict[str, Any]:
        now_str = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reading_logs (item_id, report_id, draft_title, log_title, draft_text, log_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["item_id"],
                    item["report_id"],
                    item.get("draft_title", ""),
                    log_title or item.get("log_title", "") or item.get("draft_title", ""),
                    item.get("draft_text", ""),
                    log_text,
                    now_str,
                    now_str,
                ),
            )
            conn.commit()
        return {"ok": True}

    def list_reading_logs(self, item_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if item_id:
                rows = conn.execute(
                    """
                    SELECT
                        r.*,
                        COALESCE(NULLIF(r.log_title, ''), NULLIF(r.draft_title, ''), i.title) AS display_title,
                        i.title AS item_title,
                        i.summary,
                        i.source_name,
                        i.source_url,
                        i.image_url
                    FROM reading_logs r
                    LEFT JOIN daily_report_items i ON i.item_id = r.item_id
                    WHERE r.item_id = ?
                    ORDER BY r.created_at DESC
                    """,
                    (item_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        r.*,
                        COALESCE(NULLIF(r.log_title, ''), NULLIF(r.draft_title, ''), i.title) AS display_title,
                        i.title AS item_title,
                        i.summary,
                        i.source_name,
                        i.source_url,
                        i.image_url
                    FROM reading_logs r
                    LEFT JOIN daily_report_items i ON i.item_id = r.item_id
                    ORDER BY r.created_at DESC
                    LIMIT 200
                    """
                ).fetchall()
        return [dict(row) for row in rows]
