# coding=utf-8
"""
DAY VIBE AI 的本地网页服务。

使用标准库 HTTPServer 提供静态页面和 JSON API，便于本地开发与 Docker 部署。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from trendradar.utils.time import DEFAULT_TIMEZONE, get_configured_time

from .digest import (
    build_daily_digest,
    build_reading_log_draft,
    collect_live_rss_candidates,
    load_assistant_settings,
    render_report_html,
)
from .storage import AssistantStorage


def _is_vercel_env() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


def _parse_report_time(value: str) -> tuple[int, int]:
    text = (value or "17:00").strip()
    try:
        hour_str, minute_str = text.split(":", 1)
        hour = max(0, min(23, int(hour_str)))
        minute = max(0, min(59, int(minute_str)))
        return hour, minute
    except Exception:
        return 17, 0


def _should_force_recollect(existing_report: Dict[str, Any] | None, assistant_settings: Dict[str, Any]) -> bool:
    timezone = assistant_settings.get("timezone", DEFAULT_TIMEZONE)
    now = get_configured_time(timezone)
    today = now.strftime("%Y-%m-%d")
    report_time_hour, report_time_minute = _parse_report_time(str(assistant_settings.get("report_time", "17:00")))
    passed_report_time = (now.hour, now.minute) >= (report_time_hour, report_time_minute)

    if not existing_report:
        return True

    existing_date = str(existing_report.get("report_date", "")).strip()
    if existing_date != today:
        return True

    if passed_report_time:
        return True

    return False


def _has_ai_key(assistant_settings: Dict[str, Any]) -> tuple[bool, str]:
    env_names = _candidate_api_key_env_names(assistant_settings)
    for env_name in env_names:
        if os.environ.get(env_name):
            return True, env_name
    return False, env_names[0] if env_names else "VOLC_API_KEY"


def _candidate_api_key_env_names(assistant_settings: Dict[str, Any]) -> list[str]:
    ai_cfg = assistant_settings.get("ai", {}) or {}
    preferred = str(ai_cfg.get("api_key_env", "VOLC_API_KEY")).strip() or "VOLC_API_KEY"
    ordered: list[str] = []
    for env_name in [
        preferred,
        "AI_API_KEY",
        "OPENAI_API_KEY",
        "VOLC_API_KEY",
        "ARK_API_KEY",
        "DOUBAO_API_KEY",
    ]:
        normalized = str(env_name or "").strip()
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _default_output_dir() -> Path:
    if _is_vercel_env():
        return Path(os.environ.get("DAY_VIBE_OUTPUT_DIR", "/tmp/day-vibe-ai/assistant"))
    return Path(os.environ.get("DAY_VIBE_OUTPUT_DIR", "output/assistant"))


class AssistantHTTPRequestHandler(BaseHTTPRequestHandler):
    assistant_settings = load_assistant_settings()
    _storage_instance: AssistantStorage | None = None
    dashboard_path = _default_output_dir() / "latest-report.html"
    latest_json_path = _default_output_dir() / "latest-report.json"

    @classmethod
    def _get_storage(cls) -> AssistantStorage:
        if cls._storage_instance is None:
            cls._storage_instance = AssistantStorage()
        return cls._storage_instance

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _resolve_effective_path(self, parsed) -> tuple[str, Dict[str, list[str]]]:
        query = parse_qs(parsed.query, keep_blank_values=True)
        path = parsed.path
        if path == "/api/index.py":
            forwarded = (query.get("path") or query.get("__path") or [None])[0]
            if forwarded:
                path = urlparse(forwarded).path or forwarded
            else:
                path = "/"
        return path, query

    def _ensure_latest_report(self) -> Dict[str, Any]:
        storage = self._get_storage()
        report = storage.get_latest_report()
        if report and not _should_force_recollect(report, self.assistant_settings):
            report["bookmarks"] = storage.list_bookmarks()
            report["logs"] = storage.list_reading_logs()
            return report
        injected_candidates = None
        replace_candidates = False
        if _is_vercel_env():
            injected_candidates = collect_live_rss_candidates(
                lookback_hours=int(self.assistant_settings.get("lookback_hours", 24)),
                assistant_settings=self.assistant_settings,
            )
            replace_candidates = bool(injected_candidates)
        report = build_daily_digest(
            assistant_settings=self.assistant_settings,
            storage=storage,
            injected_candidates=injected_candidates,
            replace_candidates=replace_candidates,
        )
        report["bookmarks"] = storage.list_bookmarks()
        report["logs"] = storage.list_reading_logs()
        return report

    def _find_item_in_report(self, item_id: str, report: Dict[str, Any]) -> Dict[str, Any]:
        for report_item in report.get("items", []):
            if report_item.get("item_id") == item_id:
                return dict(report_item)
        for bookmark in report.get("bookmarks", []):
            if bookmark.get("item_id") == item_id:
                return {
                    "item_id": bookmark.get("item_id", ""),
                    "report_id": bookmark.get("report_id", report.get("report_id", "")),
                    "title": bookmark.get("title", ""),
                    "summary": bookmark.get("summary", ""),
                    "source_name": bookmark.get("source_name", ""),
                    "source_url": bookmark.get("source_url", ""),
                    "image_url": bookmark.get("image_url", ""),
                    "importance": bookmark.get("importance", 0),
                    "note": bookmark.get("note", ""),
                }
        return {}

    def _serve_dashboard(self) -> None:
        report = self._ensure_latest_report()
        html = render_report_html(report)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = self._resolve_effective_path(parsed)

        if path in {"/", "/index.html"}:
            self._serve_dashboard()
            return

        if path == "/api/health":
            self._send_json({"ok": True})
            return

        if path == "/api/latest":
            report = self._ensure_latest_report()
            self._send_json({"ok": True, "report": report})
            return

        if path == "/api/runtime-check":
            storage = self._get_storage()
            latest_report = storage.get_latest_report()
            ai_key_present, ai_key_env = _has_ai_key(self.assistant_settings)
            ai_key_candidates = _candidate_api_key_env_names(self.assistant_settings)
            force_recollect = _should_force_recollect(latest_report, self.assistant_settings)
            live_probe = []
            if _is_vercel_env():
                live_probe = collect_live_rss_candidates(
                    lookback_hours=int(self.assistant_settings.get("lookback_hours", 24)),
                    assistant_settings=self.assistant_settings,
                    max_feeds=2,
                )
            self._send_json(
                {
                    "ok": True,
                    "runtime": {
                        "is_vercel": _is_vercel_env(),
                        "report_time": str(self.assistant_settings.get("report_time", "17:00")),
                        "timezone": str(self.assistant_settings.get("timezone", DEFAULT_TIMEZONE)),
                        "force_recollect_now": force_recollect,
                        "ai_key_present": ai_key_present,
                        "ai_key_env_used": ai_key_env,
                        "ai_key_candidates": ai_key_candidates,
                    },
                    "latest_report": {
                        "exists": bool(latest_report),
                        "report_date": (latest_report or {}).get("report_date", ""),
                        "generated_at": (latest_report or {}).get("generated_at", ""),
                        "item_count": len((latest_report or {}).get("items", []) or []),
                        "generator": ((latest_report or {}).get("assistant", {}) or {}).get("generator", ""),
                    },
                    "live_probe": {
                        "candidate_count": len(live_probe),
                        "sample_sources": sorted(list({item.source_name for item in live_probe}))[:5],
                    },
                }
            )
            return

        if path == "/api/reports":
            self._send_json({"ok": True, "reports": self._get_storage().list_reports()})
            return

        if path == "/api/bookmarks":
            self._send_json({"ok": True, "bookmarks": self._get_storage().list_bookmarks()})
            return

        if path == "/api/logs":
            item_id = (query.get("item_id") or [None])[0]
            self._send_json({"ok": True, "logs": self._get_storage().list_reading_logs(item_id=item_id)})
            return

        if path == "/api/item":
            item_id = (query.get("item_id") or [None])[0]
            if not item_id:
                self._send_json({"ok": False, "error": "missing item_id"}, status=400)
                return
            report = self._ensure_latest_report()
            item = self._find_item_in_report(item_id, report)
            if not item:
                self._send_json({"ok": False, "error": "item not found"}, status=404)
                return
            storage = self._get_storage()
            bookmark = storage.get_bookmark(item_id)
            logs = storage.list_reading_logs(item_id=item_id)
            self._send_json({"ok": True, "item": item, "bookmark": bookmark, "logs": logs})
            return

        if path == "/api/generate":
            report = build_daily_digest(assistant_settings=self.assistant_settings, storage=self._get_storage())
            self._send_json({"ok": True, "report": report})
            return

        if path == "/latest-report.html" and self.dashboard_path.exists():
            content = self.dashboard_path.read_text(encoding="utf-8").encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, _query = self._resolve_effective_path(parsed)
        body = self._read_json_body()

        if path == "/api/refresh":
            started_at = time.time()
            storage = self._get_storage()
            latest_report = storage.get_latest_report()
            must_recollect = _should_force_recollect(latest_report, self.assistant_settings)

            if not must_recollect:
                report = latest_report or build_daily_digest(
                    assistant_settings=self.assistant_settings,
                    storage=storage,
                )
                report["bookmarks"] = storage.list_bookmarks()
                report["logs"] = storage.list_reading_logs()
                elapsed_seconds = max(1, round(time.time() - started_at))
                self._send_json(
                    {
                        "ok": True,
                        "crawl": {
                            "success": True,
                            "mode": "cache-hit-before-cutoff",
                            "note": "当前时间在日报时间点之前，使用当日已有数据。",
                        },
                        "integration": {
                            "ai_key_present": _has_ai_key(self.assistant_settings)[0],
                            "path": "cache-hit-before-cutoff",
                        },
                        "report": report,
                        "elapsed_seconds": elapsed_seconds,
                    }
                )
                return

            crawl_result: Dict[str, Any]
            crawl_warning = ""

            if _is_vercel_env():
                live_candidates = collect_live_rss_candidates(
                    lookback_hours=int(self.assistant_settings.get("lookback_hours", 24)),
                    assistant_settings=self.assistant_settings,
                )
                crawl_result = {
                    "success": bool(live_candidates),
                    "mode": "vercel-live-rss",
                    "fetched_candidates": len(live_candidates),
                    "note": "Vercel 环境已执行实时 RSS 抓取并刷新日报。",
                }
                report = build_daily_digest(
                    assistant_settings=self.assistant_settings,
                    storage=storage,
                    injected_candidates=live_candidates,
                    replace_candidates=True,
                )
                integration_path = "vercel-live-rss"
            else:
                try:
                    from mcp_server.tools.system import SystemManagementTools

                    project_root = str(Path(__file__).resolve().parents[2])
                    crawl_result = SystemManagementTools(project_root=project_root).trigger_crawl(
                        save_to_local=True,
                        include_url=False,
                    )
                except Exception as exc:
                    crawl_warning = f"抓取失败，已回退到本地数据刷新：{exc}"
                    crawl_result = {
                        "success": False,
                        "mode": "local-fallback",
                        "note": crawl_warning,
                    }
                report = build_daily_digest(
                    assistant_settings=self.assistant_settings,
                    storage=storage,
                )
                integration_path = "local-crawler"

            if crawl_warning:
                report["brief"] = f"{crawl_warning}；{report.get('brief', '')}".strip("；")
            report["bookmarks"] = storage.list_bookmarks()
            report["logs"] = storage.list_reading_logs()
            elapsed_seconds = max(1, round(time.time() - started_at))
            self._send_json(
                {
                    "ok": True,
                    "crawl": crawl_result,
                    "integration": {
                        "ai_key_present": _has_ai_key(self.assistant_settings)[0],
                        "path": integration_path,
                        "report_generator": (report.get("assistant", {}) or {}).get("generator", ""),
                    },
                    "report": report,
                    "elapsed_seconds": elapsed_seconds,
                }
            )
            return

        if path == "/api/bookmarks":
            item_id = body.get("item_id")
            title = body.get("title", "")
            report_id = body.get("report_id", "")
            note = body.get("note", "")
            bookmarked = body.get("bookmarked")
            if isinstance(bookmarked, str):
                bookmarked = bookmarked.strip().lower() in {"1", "true", "yes", "on"}
            report = self._ensure_latest_report()
            item = self._find_item_in_report(item_id, report)
            if item_id and not item:
                item = {
                    "item_id": item_id,
                    "report_id": report_id or report.get("report_id", ""),
                    "title": title or item_id,
                }
            if not item:
                self._send_json({"ok": False, "error": "missing item_id"}, status=400)
                return
            item["report_id"] = report_id or report.get("report_id", "")
            item["title"] = title or item.get("title", "")
            if bookmarked is False:
                result = self._get_storage().remove_bookmark(item["item_id"])
            elif bookmarked is True:
                result = self._get_storage().set_bookmark(item, note=note)
            else:
                existing = self._get_storage().get_bookmark(item["item_id"])
                if existing:
                    result = self._get_storage().remove_bookmark(item["item_id"])
                else:
                    result = self._get_storage().set_bookmark(item, note=note)
            self._send_json({"ok": True, **result})
            return

        if path == "/api/logs":
            item_id = body.get("item_id")
            log_text = (body.get("log_text") or "").strip()
            draft_text = (body.get("draft_text") or "").strip()
            log_title = (body.get("log_title") or body.get("title") or "").strip()
            if not item_id or not log_text:
                self._send_json({"ok": False, "error": "item_id 和 log_text 不能为空"}, status=400)
                return
            report = self._ensure_latest_report()
            item = self._find_item_in_report(item_id, report)
            if item is None:
                item = {"item_id": item_id, "report_id": report.get("report_id", ""), "title": body.get("title", "")}
            item["report_id"] = report.get("report_id", "")
            item["draft_text"] = draft_text
            item["draft_title"] = log_title
            result = self._get_storage().add_reading_log(item, log_text, log_title=log_title)
            self._send_json({"ok": True, **result})
            return

        if path == "/api/log-draft":
            item_id = body.get("item_id")
            if not item_id:
                self._send_json({"ok": False, "error": "missing item_id"}, status=400)
                return
            report = self._ensure_latest_report()
            item = self._find_item_in_report(item_id, report)
            if not item:
                self._send_json({"ok": False, "error": "item not found"}, status=404)
                return
            draft = build_reading_log_draft(
                item=item,
                report=report,
                existing_logs=self._get_storage().list_reading_logs(item_id=item_id),
                assistant_settings=self.assistant_settings,
            )
            self._send_json({"ok": True, "draft": draft})
            return

        if path == "/api/generate":
            report = build_daily_digest(assistant_settings=self.assistant_settings, storage=self._get_storage())
            self._send_json({"ok": True, "report": report})
            return

        self._send_json({"ok": False, "error": "not found"}, status=404)


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """启动 AI 助手网页服务。"""
    _default_output_dir().mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), AssistantHTTPRequestHandler)
    print(f"[Assistant Web] 已启动: http://127.0.0.1:{port}")
    print(f"[Assistant Web] 日报生成入口: http://127.0.0.1:{port}/api/generate")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[Assistant Web] 收到退出信号，正在关闭...")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="DAY VIBE AI Web 服务")
    parser.add_argument("--host", default=os.environ.get("ASSISTANT_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEBSERVER_PORT", "8080")))
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
