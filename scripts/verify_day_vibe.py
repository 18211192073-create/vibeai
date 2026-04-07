#!/usr/bin/env python3
# coding=utf-8
"""DAY VIBE AI end-to-end smoke test.

Checks the local preview server across:
- health
- refresh flow
- latest report shape
- reading-log draft generation
- rendered HTML safety / visibility
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import sys
from pathlib import Path

import requests


BASE_URL = os.environ.get("DAYVIBE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("DAYVIBE_REQUEST_TIMEOUT_SECONDS", "360"))
SCREENSHOT_TIMEOUT_SECONDS = int(os.environ.get("DAYVIBE_SCREENSHOT_TIMEOUT_SECONDS", "20"))


def fail(message: str, details: str = "") -> None:
    print(f"[FAIL] {message}")
    if details:
        print(details)
    sys.exit(1)


def ok(message: str) -> None:
    print(f"[OK] {message}")


def get_json(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    response = requests.request(method, url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        data = response.json()
    except Exception:
        fail(f"{method} {path} did not return JSON", response.text[:1000])
    if not response.ok:
        fail(f"{method} {path} failed", json.dumps(data, ensure_ascii=False, indent=2))
    return data


def main() -> None:
    print(f"Using base url: {BASE_URL}")

    health = get_json("/api/health")
    if not health.get("ok"):
        fail("health check returned ok=false", json.dumps(health, ensure_ascii=False, indent=2))
    ok("health endpoint")

    refresh = get_json("/api/refresh", method="POST", payload={})
    report = refresh.get("report") or {}
    items = report.get("items") or []
    if not items:
        fail("refresh did not produce any report items", json.dumps(refresh, ensure_ascii=False, indent=2))
    ok(f"refresh produced {len(items)} items")

    latest = get_json("/api/latest")
    latest_report = latest.get("report") or {}
    if latest_report.get("title") != report.get("title"):
        fail("latest report title does not match refreshed report", json.dumps({"latest": latest_report.get("title"), "refresh": report.get("title")}, ensure_ascii=False, indent=2))
    ok("latest report matches refresh result")

    first_item = items[0]
    if str(first_item.get("image_url", "")).startswith("data:image/svg+xml"):
        fail("news image is still falling back to generated svg", json.dumps(first_item, ensure_ascii=False, indent=2))
    ok("news image uses a non-generated asset or is empty")

    log_draft = get_json("/api/log-draft", method="POST", payload={"item_id": first_item.get("item_id")})
    draft = log_draft.get("draft") or {}
    if draft.get("generated_by") != "volc-ark":
        fail("reading log draft is not using volc-ark", json.dumps(draft, ensure_ascii=False, indent=2))
    if len(draft.get("draft_text", "")) < 80:
        fail("reading log draft text is too short", json.dumps(draft, ensure_ascii=False, indent=2))
    if any(token in draft.get("draft_text", "") for token in ["模型洞察", "建议动作", "继续观察"]):
        fail("reading log draft still looks like a key-point list", json.dumps(draft, ensure_ascii=False, indent=2))
    ok("reading log draft is model-backed and article-like")

    html_path = Path("output/assistant/latest-report.html")
    if not html_path.exists():
        fail("latest report html not found", str(html_path))
    html = html_path.read_text(encoding="utf-8")
    for token in [
        "refreshDigest()",
        "dialog::backdrop",
        "thumb-empty",
    ]:
        if token not in html:
            fail(f"html missing expected token: {token}")
    for hidden_token in ["报告 ID", "候选数", "logDraftTips"]:
        if hidden_token in html:
            fail(f"html still exposes hidden token: {hidden_token}")
    if "data:image/svg+xml" in html:
        fail("html still includes generated svg fallback images")
    ok("rendered html contains refresh hook, dark modal styles, and no svg fallback")

    screenshot_path = Path("output/assistant/dayvibe-preview.png")
    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_started_at = time.time()
        window_id_raw = subprocess.check_output(
            [
                "osascript",
                "-e",
                'tell application "Safari" to activate',
                "-e",
                f'tell application "Safari" to open location "{BASE_URL}/"',
                "-e",
                "delay 4",
                "-e",
                'tell application "Safari" to return id of front window',
            ],
            text=True,
            timeout=SCREENSHOT_TIMEOUT_SECONDS,
        ).strip()
        window_id = "".join(ch for ch in window_id_raw if ch.isdigit())
        if not window_id:
            raise RuntimeError(f"could not determine Safari window id: {window_id_raw!r}")
        subprocess.run(
            ["screencapture", "-x", "-l", window_id, str(screenshot_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SCREENSHOT_TIMEOUT_SECONDS,
        )
        if not screenshot_path.exists() or screenshot_path.stat().st_size < 10_000:
            fail("screenshot capture produced an invalid file", str(screenshot_path))
        if screenshot_path.stat().st_mtime < screenshot_started_at - 1:
            fail("screenshot file was not refreshed during this run", str(screenshot_path))
        ok(f"screenshot captured: {screenshot_path}")
    except Exception as exc:
        print(f"[SKIP] screenshot capture unavailable: {exc}")

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
