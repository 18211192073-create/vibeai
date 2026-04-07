# coding=utf-8
"""
AI 行业日报生成器。

从现有 TrendRadar 输出中收集过去 24 小时的候选新闻，调用火山引擎 Ark 兼容接口生成日报。
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
import yaml
from json_repair import repair_json

from trendradar.ai.client import AIClient
from trendradar.utils.time import DEFAULT_TIMEZONE, get_configured_time

from .storage import AssistantStorage

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass


AI_KEYWORDS = [
    "AI",
    "人工智能",
    "大模型",
    "LLM",
    "Agent",
    "智能体",
    "OpenAI",
    "Anthropic",
    "Google DeepMind",
    "DeepSeek",
    "火山引擎",
    "豆包",
    "NVIDIA",
    "GPU",
    "推理",
    "RAG",
    "AIGC",
    "Copilot",
    "Meta AI",
]

TECH_KEYWORDS = [
    "科技",
    "技术",
    "芯片",
    "半导体",
    "算力",
    "机器人",
    "自动驾驶",
    "云计算",
    "开源",
    "编程",
    "软件",
    "模型",
    "Windows",
    "iPhone",
    "苹果",
    "谷歌",
    "微软",
    "华为",
    "阿里",
    "腾讯",
    "字节",
    "英伟达",
    "NVIDIA",
    "OpenAI",
    "DeepSeek",
    "Anthropic",
    "Google",
    "Meta",
]

EXCLUDE_KEYWORDS = [
    "娱乐",
    "明星",
    "八卦",
    "综艺",
    "影视",
    "体育",
    "足球",
    "篮球",
    "情感",
    "旅游",
    "美食",
    "健康",
    "社会",
    "国际",
    "战争",
]

FINANCE_NOISE_KEYWORDS = [
    "美股",
    "港股",
    "A股",
    "股价",
    "开盘",
    "收盘",
    "涨幅",
    "跌幅",
    "指数",
    "板块",
    "财报",
    "业绩",
    "估值",
    "市值",
    "基金",
    "债券",
    "期货",
    "券商",
    "银行",
]

WEAK_BRAND_KEYWORDS = [
    "Windows",
    "iPhone",
    "苹果",
    "谷歌",
    "微软",
    "华为",
    "阿里",
    "腾讯",
    "字节",
    "英伟达",
    "Meta",
]

SOURCE_PRIORITY_RULES = [
    ("openai blog", 24.0),
    ("openai x", 23.0),
    ("openai", 22.0),
    ("anthropic", 20.0),
    ("google deepmind", 20.0),
    ("deepmind", 18.0),
    ("meta ai", 17.0),
    ("the verge", 14.0),
    ("wired", 14.0),
    ("axios", 14.0),
    ("google", 12.0),
    ("techcrunch", 10.0),
    ("hacker news", 8.0),
    ("zhihu", -12.0),
    ("weibo", -8.0),
    ("douyin", -8.0),
    ("bilibili", -5.0),
]

TECH_CONTEXT_KEYWORDS = [
    "发布",
    "上线",
    "试产",
    "产品",
    "模型",
    "系统",
    "应用",
    "平台",
    "工具",
    "服务",
    "软件",
    "芯片",
    "算力",
    "开发者",
    "开源",
    "AI",
    "人工智能",
    "大模型",
    "Agent",
    "智能体",
    "机器人",
    "自动驾驶",
    "云计算",
    "代码",
]

_AUTO_REFRESH_ATTEMPTED = False


def _default_output_dir() -> Path:
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
        return Path(os.environ.get("DAY_VIBE_OUTPUT_DIR", "/tmp/day-vibe-ai/assistant"))
    return Path(os.environ.get("DAY_VIBE_OUTPUT_DIR", "output/assistant"))


def _is_vercel_env() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


def _resolve_ai_api_key(*env_names: str) -> str:
    for name in env_names:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


@dataclass
class DigestCandidate:
    item_id: str
    source_type: str
    source_name: str
    title: str
    original_title: str
    summary: str
    source_url: str
    image_url: str
    published_at: str
    importance_hint: float
    raw: Dict[str, Any]


def load_assistant_settings(path: str = "config/assistant.yaml") -> Dict[str, Any]:
    """加载助手配置，文件缺失时使用默认值。"""
    default = {
        "enabled": True,
        "app_name": "DAY VIBE AI",
        "report_time": "17:00",
        "timezone": DEFAULT_TIMEZONE,
        "lookback_hours": 24,
        "candidate_limit": 60,
        "max_items": 8,
        "title_style": "analysis",
        "sources": {
            "platform_ids": [
                "wallstreetcn-hot",
                "zhihu",
                "weibo",
                "douyin",
                "bilibili-hot-search",
                "thepaper",
                "hacker-news",
                "yahoo-finance",
            ],
            "rss_ids": [
                "hacker-news",
                "yahoo-finance",
            ],
            "title_keywords": AI_KEYWORDS,
        },
        "source_priority": {
            "enabled": True,
        },
        "feishu": {
            "enabled": False,
            "webhook_url": "",
            "dashboard_url": "http://127.0.0.1:8080",
        },
        "ai": {
            "model": "openai/doubao-seed-2-0-code-preview-260215",
            "api_base": "https://ark.cn-beijing.volces.com/api/v3",
            "api_key_env": "VOLC_API_KEY",
            "temperature": 0.35,
            "max_tokens": 3500,
            "timeout": 120,
        },
        "reading_log": {
            "enabled": True,
            "prompt_file": "config/ai_reading_log_prompt.txt",
            "temperature": 0.55,
            "max_tokens": 1800,
        },
    }

    path_obj = Path(path)
    if not path_obj.exists():
        return default

    data = yaml.safe_load(path_obj.read_text(encoding="utf-8")) or {}
    merged = default | data
    merged["sources"] = default["sources"] | (data.get("sources", {}) or {})
    merged["feishu"] = default["feishu"] | (data.get("feishu", {}) or {})
    merged["ai"] = default["ai"] | (data.get("ai", {}) or {})
    merged["reading_log"] = default["reading_log"] | (data.get("reading_log", {}) or {})
    return merged


def _safe_parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value[:19], fmt)
        except Exception:
            continue
    return None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fa5]+", "-", text.lower()).strip("-")
    return cleaned[:40] or "digest"


def _make_item_id(*parts: str) -> str:
    joined = "||".join(_normalize_text(p) for p in parts if p is not None)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _contains_any(text: str, keywords: List[str]) -> bool:
    lowered = (text or "").lower()
    return any(keyword and keyword.lower() in lowered for keyword in keywords)


def _source_priority_boost(source_name: str, source_id: str, source_priority: Optional[Dict[str, Any]] = None) -> float:
    source_priority = source_priority or {}
    if source_priority.get("enabled", True) is False:
        return 0.0
    normalized = f"{source_name} {source_id}".lower()
    boost = 0.0
    for pattern, score in SOURCE_PRIORITY_RULES:
        if pattern and pattern in normalized:
            boost = max(boost, float(score))
    return boost


def _is_tech_source(source_name: str) -> bool:
    tech_source_keywords = [
        "科技",
        "tech",
        "it",
        "hacker news",
        "the verge",
        "wired",
        "axios",
        "openai",
        "anthropic",
        "google deepmind",
        "deepmind",
        "meta ai",
        "techcrunch",
        "infoq",
        "极客",
        "机器之心",
        "量子位",
        "少数派",
        "36氪",
        "钛媒体",
        "虎嗅",
        "arxiv",
    ]
    return _contains_any(source_name, tech_source_keywords)


def _is_ai_related(title: str, source_name: str, source_id: str, assistant_sources: Dict[str, Any]) -> bool:
    platform_ids = set(assistant_sources.get("platform_ids", []))
    rss_ids = set(assistant_sources.get("rss_ids", []))
    title_keywords = assistant_sources.get("title_keywords", []) or []
    tech_keywords = assistant_sources.get("tech_keywords", []) or []
    exclude_keywords = assistant_sources.get("exclude_keywords", []) or []

    combined_keywords = list(dict.fromkeys([*title_keywords, *tech_keywords, *AI_KEYWORDS, *TECH_KEYWORDS]))
    text_blob = f"{title} {source_name}"

    if _contains_any(text_blob, exclude_keywords):
        return False

    if _contains_any(title, FINANCE_NOISE_KEYWORDS) and not _contains_any(title, AI_KEYWORDS):
        return False

    strong_ai_hit = _contains_any(title, AI_KEYWORDS)
    strong_tech_hit = _contains_any(title, ["芯片", "半导体", "算力", "机器人", "自动驾驶", "云计算", "开源", "编程", "软件", "模型", "RAG", "AIGC", "推理"])
    weak_brand_hit = _contains_any(title, WEAK_BRAND_KEYWORDS)
    tech_context_hit = _contains_any(title, TECH_CONTEXT_KEYWORDS)
    source_bias = source_id in platform_ids or source_id in rss_ids
    source_hint = _is_tech_source(source_name)

    if strong_ai_hit or strong_tech_hit:
        return True

    if weak_brand_hit and tech_context_hit:
        return True

    if source_hint and (_contains_any(title, combined_keywords) or tech_context_hit):
        return True

    if source_bias and source_hint:
        return True

    return False


def _truncate(text: str, limit: int = 220) -> str:
    text = _normalize_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _extract_image_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={
                "User-Agent": "Mozilla/5.0 (DAY VIBE AI Assistant)",
                "Accept": "text/html,application/xhtml+xml,application/xml",
            },
        )
        if response.status_code != 200:
            return ""
        html_text = response.text
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text, flags=re.I)
            if match:
                return urljoin(url, match.group(1).strip())
    except Exception:
        return ""
    return ""


def _fallback_svg(title: str, source_name: str) -> str:
    safe_title = html.escape(_truncate(title, 42))
    safe_source = html.escape(_truncate(source_name, 24))
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675">
      <defs>
        <linearGradient id="g" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0%" stop-color="#0f172a"/>
          <stop offset="55%" stop-color="#1d4ed8"/>
          <stop offset="100%" stop-color="#7c3aed"/>
        </linearGradient>
      </defs>
      <rect width="1200" height="675" rx="36" fill="url(#g)"/>
      <circle cx="1040" cy="120" r="180" fill="rgba(255,255,255,0.09)"/>
      <circle cx="180" cy="560" r="220" fill="rgba(255,255,255,0.06)"/>
      <text x="72" y="120" fill="rgba(255,255,255,0.72)" font-size="28" font-family="Arial, sans-serif">DAY VIBE AI</text>
      <text x="72" y="260" fill="white" font-size="56" font-weight="700" font-family="Arial, sans-serif">{safe_title}</text>
      <text x="72" y="340" fill="rgba(255,255,255,0.78)" font-size="30" font-family="Arial, sans-serif">{safe_source}</text>
      <text x="72" y="610" fill="rgba(255,255,255,0.54)" font-size="22" font-family="Arial, sans-serif">DAY VIBE AI Assistant</text>
    </svg>
    """.strip()
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _load_prompt(path: str = "config/ai_assistant_prompt.txt") -> str:
    prompt_path = Path(path)
    if not prompt_path.exists():
        return ""
    return prompt_path.read_text(encoding="utf-8")


def _load_sectioned_prompt(path: str, default_system: str, default_user: str) -> Tuple[str, str]:
    """读取带 [system]/[user] 分段的 prompt 文件。"""
    prompt = _load_prompt(path)
    if not prompt:
        return default_system, default_user

    system_parts: List[str] = []
    user_parts: List[str] = []
    current = None
    for raw_line in prompt.splitlines():
        line = raw_line.rstrip()
        if line.strip().lower() == "[system]":
            current = "system"
            continue
        if line.strip().lower() == "[user]":
            current = "user"
            continue
        if current == "system":
            system_parts.append(line)
        elif current == "user":
            user_parts.append(line)

    system_text = "\n".join(system_parts).strip() or default_system
    user_text = "\n".join(user_parts).strip() or default_user
    return system_text, user_text


def _collect_from_sqlite(
    db_path: Path,
    table: str,
    source_type: str,
    source_priority: Optional[Dict[str, Any]] = None,
) -> List[DigestCandidate]:
    if not db_path.exists():
        return []

    import sqlite3

    candidates: List[DigestCandidate] = []
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if table == "news_items":
                rows = conn.execute(
                    """
                    SELECT n.title, n.url, n.mobile_url, n.created_at, n.updated_at,
                           n.first_crawl_time, n.last_crawl_time, n.rank,
                           p.name AS source_name, p.id AS source_id
                    FROM news_items n
                    LEFT JOIN platforms p ON n.platform_id = p.id
                    """
                ).fetchall()
                for row in rows:
                    title = _normalize_text(row["title"] or "")
                    source_name = row["source_name"] or row["source_id"] or "热榜"
                    source_id = row["source_id"] or "unknown"
                    url = row["mobile_url"] or row["url"] or ""
                    image_url = ""
                    importance_hint = float(max(0, 100 - int(row["rank"] or 50)))
                    importance_hint += _source_priority_boost(source_name, source_id, source_priority)
                    candidates.append(
                        DigestCandidate(
                            item_id=_make_item_id(source_type, source_id, url, title),
                            source_type="hotlist",
                            source_name=source_name,
                            title=title,
                            original_title=title,
                            summary=title,
                            source_url=url,
                            image_url=image_url,
                            published_at=row["updated_at"] or row["created_at"] or "",
                            importance_hint=importance_hint,
                            raw=dict(row),
                        )
                    )
            elif table == "rss_items":
                rows = conn.execute(
                    """
                    SELECT r.title, r.url, r.summary, r.published_at, r.created_at, r.updated_at,
                           f.name AS source_name, f.id AS source_id
                    FROM rss_items r
                    LEFT JOIN rss_feeds f ON r.feed_id = f.id
                    """
                ).fetchall()
                for row in rows:
                    title = _normalize_text(row["title"] or "")
                    source_name = row["source_name"] or row["source_id"] or "RSS"
                    source_id = row["source_id"] or "unknown"
                    url = row["url"] or ""
                    image_url = ""
                    summary = _truncate(row["summary"] or title, 220)
                    importance_hint = 70.0 if _is_ai_related(title, source_name, source_id, {"platform_ids": [], "rss_ids": []}) else 40.0
                    importance_hint += _source_priority_boost(source_name, source_id, source_priority)
                    candidates.append(
                        DigestCandidate(
                            item_id=_make_item_id(source_type, source_id, url, title),
                            source_type="rss",
                            source_name=source_name,
                            title=title,
                            original_title=title,
                            summary=summary,
                            source_url=url,
                            image_url=image_url,
                            published_at=row["published_at"] or row["updated_at"] or row["created_at"] or "",
                            importance_hint=importance_hint,
                            raw=dict(row),
                        )
                    )
    except Exception:
        return []

    return candidates


def collect_candidates(lookback_hours: int = 24, assistant_settings: Optional[Dict[str, Any]] = None) -> List[DigestCandidate]:
    """从现有数据库中收集过去 lookback_hours 小时的候选新闻。"""
    assistant_settings = assistant_settings or {}
    assistant_sources = assistant_settings.get("sources", {}) or {}
    source_priority = assistant_settings.get("source_priority", {}) or {}
    now = datetime.now()
    cutoff = now - timedelta(hours=lookback_hours)
    output_dir = Path("output")
    candidates: List[DigestCandidate] = []

    for db_path in sorted((output_dir / "news").glob("*.db")):
        for candidate in _collect_from_sqlite(db_path, "news_items", "hotlist", source_priority=source_priority):
            if _safe_parse_datetime(candidate.published_at) and _safe_parse_datetime(candidate.published_at) < cutoff:
                continue
            if _is_ai_related(candidate.title, candidate.source_name, candidate.raw.get("source_id", ""), assistant_sources):
                candidates.append(candidate)

    for db_path in sorted((output_dir / "rss").glob("*.db")):
        for candidate in _collect_from_sqlite(db_path, "rss_items", "rss", source_priority=source_priority):
            if _safe_parse_datetime(candidate.published_at) and _safe_parse_datetime(candidate.published_at) < cutoff:
                continue
            if _is_ai_related(candidate.title, candidate.source_name, candidate.raw.get("source_id", ""), assistant_sources):
                candidates.append(candidate)

    # 去重：按 item_id 保留最新的一条
    dedup: Dict[str, DigestCandidate] = {}
    for candidate in candidates:
        dedup[candidate.item_id] = candidate

    return sorted(dedup.values(), key=lambda item: (item.importance_hint, item.published_at), reverse=True)


def collect_live_rss_candidates(
    lookback_hours: int = 24,
    assistant_settings: Optional[Dict[str, Any]] = None,
    max_feeds: int = 4,
) -> List[DigestCandidate]:
    """在线实时抓取 RSS 候选（用于 Vercel 刷新）。"""
    assistant_settings = assistant_settings or {}
    assistant_sources = assistant_settings.get("sources", {}) or {}
    source_priority = assistant_settings.get("source_priority", {}) or {}
    timezone = assistant_settings.get("timezone", DEFAULT_TIMEZONE)
    rss_whitelist = set(assistant_sources.get("rss_ids", []) or [])

    config_path = Path("config/config.yaml")
    if not config_path.exists():
        return []
    try:
        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []

    rss_cfg = config_data.get("rss", {}) or config_data.get("RSS", {}) or {}
    feeds = [feed for feed in (rss_cfg.get("feeds", []) or []) if feed.get("enabled", True)]
    if rss_whitelist:
        feeds = [feed for feed in feeds if feed.get("id") in rss_whitelist]

    preferred_ids = [
        "openai-news",
        "anthropic-news",
        "deepmind-blog",
        "google-blog",
        "meta-tech-innovation",
        "meta-product-news",
        "the-verge",
        "wired-ai",
        "hacker-news",
        "axios",
    ]
    order = {feed_id: idx for idx, feed_id in enumerate(preferred_ids)}
    feeds.sort(key=lambda feed: order.get(feed.get("id", ""), 999))
    feeds = feeds[:max_feeds]
    if not feeds:
        return []

    try:
        from trendradar.crawler.rss.fetcher import RSSFetcher
    except Exception:
        return []

    runtime_rss_config = {
        "feeds": feeds,
        "request_interval": 50,
        "timeout": 4,
        "use_proxy": False,
        "timezone": timezone,
        "freshness_filter": {"enabled": True, "max_age_days": 2},
    }

    try:
        fetcher = RSSFetcher.from_config(runtime_rss_config)
        rss_data = fetcher.fetch_all()
    except Exception:
        return []

    now = get_configured_time(timezone)
    cutoff = now - timedelta(hours=lookback_hours)
    candidates: List[DigestCandidate] = []

    for feed_id, items in (rss_data.items or {}).items():
        for rss_item in items or []:
            title = _normalize_text(getattr(rss_item, "title", "") or "")
            source_name = getattr(rss_item, "feed_name", "") or feed_id or "RSS"
            source_url = getattr(rss_item, "url", "") or ""
            summary = _truncate(getattr(rss_item, "summary", "") or title, 220)
            published_at = getattr(rss_item, "published_at", "") or ""

            parsed_time = _safe_parse_datetime(published_at)
            if parsed_time and parsed_time < cutoff:
                continue
            if not _is_ai_related(title, source_name, feed_id, assistant_sources):
                continue

            importance_hint = 70.0 + _source_priority_boost(source_name, feed_id, source_priority)
            candidates.append(
                DigestCandidate(
                    item_id=_make_item_id("rss-live", feed_id, source_url, title),
                    source_type="rss",
                    source_name=source_name,
                    title=title,
                    original_title=title,
                    summary=summary,
                    source_url=source_url,
                    image_url="",
                    published_at=published_at,
                    importance_hint=importance_hint,
                    raw={
                        "source_id": feed_id,
                        "author": getattr(rss_item, "author", "") or "",
                        "crawl_time": getattr(rss_item, "crawl_time", "") or "",
                    },
                )
            )

    dedup: Dict[str, DigestCandidate] = {}
    for candidate in candidates:
        dedup[candidate.item_id] = candidate
    return sorted(dedup.values(), key=lambda item: (item.importance_hint, item.published_at), reverse=True)


def _attempt_auto_refresh(assistant_settings: Dict[str, Any]) -> Dict[str, Any]:
    """在日报为空时尝试自动补跑一次采集。"""
    global _AUTO_REFRESH_ATTEMPTED

    if _is_vercel_env():
        return {
            "attempted": False,
            "success": False,
            "message": "Vercel 环境跳过自动补采",
        }

    if _AUTO_REFRESH_ATTEMPTED or not assistant_settings.get("auto_refresh_on_empty", True):
        return {
            "attempted": False,
            "success": False,
            "message": "自动补采未执行",
        }

    _AUTO_REFRESH_ATTEMPTED = True
    try:
        from mcp_server.tools.system import SystemManagementTools

        project_root = str(Path(__file__).resolve().parents[2])
        result = SystemManagementTools(project_root=project_root).trigger_crawl(
            save_to_local=True,
            include_url=False,
        )
        summary = result.get("summary", {}) if isinstance(result, dict) else {}
        return {
            "attempted": True,
            "success": bool(result.get("success")),
            "message": summary.get("status") or result.get("note") or "自动补采已执行",
            "total_news": summary.get("total_news", 0),
            "saved_to_local": summary.get("saved_to_local", False),
        }
    except Exception as exc:
        return {
            "attempted": True,
            "success": False,
            "message": f"自动补采失败: {exc}",
        }


def _heuristic_digest(
    candidates: List[DigestCandidate],
    max_items: int,
    app_name: str = "DAY VIBE AI",
    empty_hint: str = "",
) -> Dict[str, Any]:
    selected = candidates[:max_items]
    items = []
    for idx, candidate in enumerate(selected, start=1):
        items.append(
            {
                "item_id": candidate.item_id,
                "title": candidate.title,
                "summary": _truncate(candidate.summary, 500),
                "importance": max(50, int(candidate.importance_hint)),
                "importance_reason": "基于来源权重、时间新鲜度和 AI 相关关键词的启发式排序",
                "source_name": candidate.source_name,
                "source_type": candidate.source_type,
                "source_url": candidate.source_url,
                "image_url": candidate.image_url or "",
                "published_at": candidate.published_at,
                "original_title": candidate.original_title,
            }
        )
    themes = _derive_themes_from_candidates(selected)
    return {
        "daily_title": f"{app_name} 24 小时速览",
        "brief": empty_hint or "当前为启发式兜底模式，模型暂不可用或未生成结构化结果。",
        "themes": themes,
        "items": items,
    }


def _derive_themes_from_candidates(candidates: List[DigestCandidate]) -> List[str]:
    keywords = [
        ("模型", ["模型", "大模型", "LLM", "OpenAI", "DeepSeek", "Anthropic", "Google DeepMind"]),
        ("Agent", ["Agent", "智能体"]),
        ("芯片与算力", ["芯片", "算力", "GPU", "NVIDIA", "英伟达", "半导体"]),
        ("开源生态", ["开源", "RAG", "GitHub", "社区"]),
        ("产品发布", ["发布", "上线", "产品", "工具", "应用"]),
        ("政策与监管", ["政策", "监管", "合规", "禁令"]),
        ("科技公司动态", ["微软", "谷歌", "苹果", "华为", "阿里", "腾讯", "字节", "Meta"]),
    ]
    collected: List[str] = []
    titles_blob = " ".join([candidate.title for candidate in candidates])
    for label, matches in keywords:
        if _contains_any(titles_blob, matches):
            collected.append(label)
    return collected[:5] or ["AI/科技"]


def _build_prompt(
    candidates: List[DigestCandidate],
    max_items: int,
    assistant_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    assistant_settings = assistant_settings or {}
    prompt_file = assistant_settings.get("prompt_file") or "config/ai_assistant_prompt.txt"
    default_system = textwrap.dedent(
        """
        你是一名资深 AI/科技 行业编辑，负责把过去 24 小时内的候选新闻整理成一份高质量日报。
        你必须严格输出 JSON，禁止 Markdown、禁止代码块、禁止多余解释。
        你要从候选列表中挑选最重要的前 {max_items} 条，按重要性从高到低排序。
        每条新闻需要给出中文摘要，摘要不超过 500 字。
        标题可以保留英文原文，不要强行翻译品牌名和模型名。
        日报标题要简洁、带一点洞察感，但不要夸张。
        """.strip()
    )
    default_user = textwrap.dedent(
        """
        请根据下面候选新闻生成 AI/科技 行业日报。

        输出 JSON 格式：
        {
          "daily_title": "日报总标题",
          "brief": "当天总述，2-3 句",
          "themes": ["主题1", "主题2", "主题3"],
          "items": [
            {
              "item_id": "候选 id",
              "title": "原始标题，尽量保留候选标题本身",
              "display_title": "给前端展示用的中文标题；如果原始标题已经是中文，可与 title 相同",
              "summary": "中文摘要，不超过500字",
              "importance": 95,
              "importance_reason": "为什么重要，1-2 句"
            }
          ]
        }

        规则：
        1. 只返回 JSON，不能出现 ``` 或额外说明。
        2. 按重要性从高到低排序，最多输出 {max_items} 条。
        3. 优先选择对 AI / 科技 行业影响更大的内容，例如基础模型、Agent、芯片、算力、开源生态、政策、融资、产品发布。
        4. 如果候选里有重复或高度相似内容，只保留最值得读的一条。
        5. 摘要必须是中文，客观、简洁、有信息量。
        6. item_id 必须来自候选新闻。
        7. 来源优先级：英文权威源优先，尤其是 OpenAI Blog / X、Anthropic、Google DeepMind、Meta AI、The Verge、WIRED、Axios、Google；知乎应明显降权，除非它提供了非常明确的一手 AI / 科技 信息。
        8. 如果候选里存在非 AI/科技 内容，直接剔除，不要凑数。
        9. 对英文来源新闻，请额外生成自然、准确、像中文科技媒体会使用的 display_title，用于前端展示；品牌名、模型名、产品名保留原文。

        候选新闻：
        {items_json}
        """
    ).strip()

    system_prompt, user_prompt = _load_sectioned_prompt(prompt_file, default_system, default_user)
    system_prompt = system_prompt.replace("{max_items}", str(max_items))

    items_payload = [
        {
            "item_id": item.item_id,
            "title": item.title,
            "summary": item.summary,
            "source_name": item.source_name,
            "source_type": item.source_type,
            "source_url": item.source_url,
            "published_at": item.published_at,
            "importance_hint": item.importance_hint,
            "source_priority": _source_priority_boost(item.source_name, item.raw.get("source_id", ""), assistant_settings.get("source_priority", {})),
        }
        for item in candidates
    ]

    user_prompt = user_prompt.replace("{max_items}", str(max_items))
    user_prompt = user_prompt.replace("{items_json}", json.dumps(items_payload, ensure_ascii=False, indent=2))
    return system_prompt, user_prompt


def _parse_json_response(response_text: str) -> Dict[str, Any]:
    try:
        return json.loads(response_text)
    except Exception:
        repaired = repair_json(response_text, return_objects=True)
        if isinstance(repaired, dict):
            return repaired
        if isinstance(repaired, str):
            return json.loads(repaired)
    raise ValueError("无法解析模型返回的 JSON")


def _build_reading_log_prompt(
    item: Dict[str, Any],
    report: Dict[str, Any],
    existing_logs: List[Dict[str, Any]],
    assistant_settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    assistant_settings = assistant_settings or {}
    reading_cfg = assistant_settings.get("reading_log", {})
    prompt_file = reading_cfg.get("prompt_file") or "config/ai_reading_log_prompt.txt"
    default_system = textwrap.dedent(
        """
        你是一名资深 AI/科技 行业研究员，负责基于一篇新闻和已有阅读记录，生成一段有洞察力、前沿、可行动的阅读日志初稿。
        要求：
        1. 面向 AI / 科技 语境，不要写泛泛而谈的鸡汤。
        2. 语言要像一个懂行业的人在做阅读记录，重点回答“这意味着什么”“为什么值得持续关注”“我接下来要看什么”。
        3. 输出要简洁、具体、可编辑，避免空话和重复标题内容。
        4. 如果给定了旧阅读记录，要自然吸收，不要重复已经写过的判断。
        5. 严格输出 JSON，不要 Markdown、代码块或额外说明。
        """.strip()
    )
    default_user = textwrap.dedent(
        """
        请根据下面的新闻和历史阅读记录生成一段阅读日志初稿。

        输出格式：
        {
          "draft_title": "日志标题",
          "draft_text": "一段 120-300 字的中文日志正文",
          "insights": ["洞察1", "洞察2", "洞察3"],
          "action_items": ["下一步动作1", "下一步动作2"],
          "questions_to_watch": ["后续值得继续观察的问题1", "问题2"]
        }

        输入新闻：
        {item_json}

        日报上下文：
        {report_json}

        已有阅读记录：
        {existing_logs_json}

        要求：
        - 只返回 JSON。
        - draft_text 要可直接编辑保存。
        - insights / action_items / questions_to_watch 各给 2-3 条，尽量具体。
        - 如果这条新闻更偏产品、模型、芯片、开源或政策，请直接点明其行业意义。
        """
    ).strip()
    system_prompt, user_prompt = _load_sectioned_prompt(prompt_file, default_system, default_user)
    return system_prompt, user_prompt


def build_reading_log_draft(
    item: Dict[str, Any],
    report: Dict[str, Any],
    existing_logs: Optional[List[Dict[str, Any]]] = None,
    assistant_settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """为指定新闻生成阅读日志初稿。"""
    assistant_settings = assistant_settings or load_assistant_settings()
    reading_cfg = assistant_settings.get("reading_log", {})
    existing_logs = existing_logs or []

    item_json = json.dumps(item, ensure_ascii=False, indent=2)
    report_json = json.dumps(
        {
            "report_id": report.get("report_id", ""),
            "title": report.get("title", ""),
            "brief": report.get("brief", ""),
            "themes": report.get("themes", []),
            "generated_at": report.get("generated_at", ""),
        },
        ensure_ascii=False,
        indent=2,
    )
    existing_logs_json = json.dumps(existing_logs[-3:], ensure_ascii=False, indent=2)

    system_prompt, user_prompt = _build_reading_log_prompt(
        item=item,
        report=report,
        existing_logs=existing_logs,
        assistant_settings=assistant_settings,
    )
    user_prompt = user_prompt.replace("{item_json}", item_json)
    user_prompt = user_prompt.replace("{report_json}", report_json)
    user_prompt = user_prompt.replace("{existing_logs_json}", existing_logs_json)

    api_key = _resolve_ai_api_key(
        reading_cfg.get("api_key_env", assistant_settings.get("ai", {}).get("api_key_env", "VOLC_API_KEY")),
        assistant_settings.get("ai", {}).get("api_key_env", "VOLC_API_KEY"),
        "AI_API_KEY",
        "OPENAI_API_KEY",
        "VOLC_API_KEY",
    )
    if not api_key:
        summary_text = _truncate(item.get("summary", "") or item.get("title", ""), 220)
        return {
            "draft_title": f"读 {item.get('title', '这条新闻')}：我的观察",
            "draft_text": f"这条新闻表面上看是一次普通的信息更新，但放在 {item.get('source_name', 'AI/科技')} 语境里，它更像是一个能看出趋势走向的信号。{summary_text}。如果把它放在整个 AI/科技 行业链条里看，我更在意的是它接下来会不会继续扩散成产品调整、能力升级或者新的实践路径。比起单条新闻本身，更值得观察的是它是否会让更多厂商、开发者或者用户开始重新判断这类方向的边界和价值。",
            "insights": [
                "这不是一条孤立新闻，更像是一个趋势信号。",
                "需要结合行业上下游变化看它的实际影响。",
                "接下来更值得观察落地节奏和后续动作。",
            ],
            "action_items": [
                "跟踪后续官方发布或二次报道",
                "关注是否影响产品、模型或基础设施迭代",
            ],
            "questions_to_watch": [
                "它会不会带来新的产品化路径？",
                "相关公司会如何把这件事落到下一步？",
            ],
            "generated_by": "heuristic",
        }

    ai_client = AIClient(
        {
            "MODEL": reading_cfg.get("model", assistant_settings.get("ai", {}).get("model", "openai/doubao-seed-2-0-code-preview-260215")),
            "API_KEY": api_key,
            "API_BASE": reading_cfg.get("api_base", assistant_settings.get("ai", {}).get("api_base", "https://ark.cn-beijing.volces.com/api/v3")),
            "TIMEOUT": reading_cfg.get("timeout", assistant_settings.get("ai", {}).get("timeout", 120)),
            "TEMPERATURE": reading_cfg.get("temperature", 0.55),
            "MAX_TOKENS": reading_cfg.get("max_tokens", 1800),
            "NUM_RETRIES": 2,
            "FALLBACK_MODELS": [],
        }
    )
    try:
        response = ai_client.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        parsed = _parse_json_response(response)
        draft_text = parsed.get("draft_text") or ""
        if not draft_text:
            draft_text = f"这条新闻不是单点事件，更像是整个 {item.get('source_name', 'AI/科技')} 行业变化的一截切面。把它放进更大的行业背景里看，它背后体现的是产品、技术和用户预期之间的重新校准。接下来我更关注的不是这条消息本身，而是它会不会继续推动新的落地方式、竞争节奏和行业判断发生变化。"
        return {
            "draft_title": parsed.get("draft_title") or f"读 {item.get('title', '这条新闻')}：我的观察",
            "draft_text": _truncate(draft_text, 1200),
            "insights": parsed.get("insights") or [],
            "action_items": parsed.get("action_items") or [],
            "questions_to_watch": parsed.get("questions_to_watch") or [],
            "generated_by": "volc-ark",
        }
    except Exception as exc:
        fallback = _truncate(item.get("summary", "") or item.get("title", ""), 220)
        return {
            "draft_title": f"读 {item.get('title', '这条新闻')}：我的观察",
            "draft_text": f"这条新闻的核心信息是：{fallback}。但如果只停留在事实层面，其实还看不出它真正的分量。把它放在 AI/科技 产业链里看，我更关注的是它背后反映出的行业选择、能力边界和落地节奏。很多时候，一条新闻并不只是告诉我们发生了什么，更重要的是它在提醒我们，下一步可能会往哪里走。",
            "insights": [
                f"模型生成失败，已回退到启发式初稿：{exc}",
            ],
            "action_items": [
                "把这条新闻补充成你的个人判断",
            ],
            "questions_to_watch": [
                "后续是否会有更明确的产品或数据支撑？",
            ],
            "generated_by": "heuristic_fallback",
        }


def _attach_assets(item: Dict[str, Any], candidate_map: Dict[str, DigestCandidate]) -> Dict[str, Any]:
    candidate = candidate_map.get(item.get("item_id", ""))
    if not candidate:
        return item
    merged = dict(item)
    merged.setdefault("title", candidate.title)
    merged.setdefault("display_title", merged.get("title", candidate.title))
    merged.setdefault("original_title", candidate.original_title)
    merged.setdefault("source_name", candidate.source_name)
    merged.setdefault("source_type", candidate.source_type)
    merged.setdefault("source_url", candidate.source_url)
    merged.setdefault("published_at", candidate.published_at)
    if not merged.get("image_url") and candidate.source_url:
        merged["image_url"] = _extract_image_from_url(candidate.source_url)
    if not merged.get("image_url"):
        merged["image_url"] = candidate.image_url or ""
    return merged


def build_daily_digest(
    assistant_config_path: str = "config/assistant.yaml",
    assistant_settings: Optional[Dict[str, Any]] = None,
    storage: Optional[AssistantStorage] = None,
    injected_candidates: Optional[List[DigestCandidate]] = None,
    replace_candidates: bool = False,
) -> Dict[str, Any]:
    """生成日报、保存到本地，并返回结构化数据。"""
    assistant_settings = assistant_settings or load_assistant_settings(assistant_config_path)
    storage = storage or AssistantStorage()
    app_name = str(assistant_settings.get("app_name", "DAY VIBE AI"))
    empty_hint = "当前没有可用候选新闻，请先运行采集任务。"
    if _is_vercel_env():
        empty_hint = "当前没有可用候选新闻，正在尝试实时抓取最新 AI/科技 RSS 数据。"

    lookback_hours = int(assistant_settings.get("lookback_hours", 24))
    max_items = int(assistant_settings.get("max_items", 8))
    candidates = collect_candidates(lookback_hours=lookback_hours, assistant_settings=assistant_settings)
    if injected_candidates:
        if replace_candidates:
            candidates = list(injected_candidates)
        else:
            merged: Dict[str, DigestCandidate] = {item.item_id: item for item in candidates}
            for item in injected_candidates:
                merged[item.item_id] = item
            candidates = sorted(merged.values(), key=lambda item: (item.importance_hint, item.published_at), reverse=True)
    elif not candidates and _is_vercel_env():
        live_candidates = collect_live_rss_candidates(
            lookback_hours=lookback_hours,
            assistant_settings=assistant_settings,
        )
        if live_candidates:
            candidates = live_candidates
            empty_hint = f"已实时抓取并生成 {len(candidates)} 条候选新闻。"

    now = get_configured_time(assistant_settings.get("timezone", DEFAULT_TIMEZONE))
    report_date = now.strftime("%Y-%m-%d")
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    window_start = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d %H:%M:%S")
    window_end = generated_at

    candidate_map = {item.item_id: item for item in candidates}

    ai_cfg = assistant_settings.get("ai", {})
    api_key = _resolve_ai_api_key(
        ai_cfg.get("api_key_env", "VOLC_API_KEY"),
        "AI_API_KEY",
        "OPENAI_API_KEY",
        "VOLC_API_KEY",
    )
    if not api_key:
        result = _heuristic_digest(candidates, max_items, app_name=app_name, empty_hint=empty_hint)
    else:
        ai_client = AIClient(
            {
                "MODEL": ai_cfg.get("model", "openai/doubao-seed-2-0-code-preview-260215"),
                "API_KEY": api_key,
                "API_BASE": ai_cfg.get("api_base", "https://ark.cn-beijing.volces.com/api/v3"),
                "TIMEOUT": ai_cfg.get("timeout", 120),
                "TEMPERATURE": ai_cfg.get("temperature", 0.35),
                "MAX_TOKENS": ai_cfg.get("max_tokens", 3500),
                "NUM_RETRIES": 2,
                "FALLBACK_MODELS": [],
            }
        )
        system_prompt, user_prompt = _build_prompt(
            candidates[: int(assistant_settings.get("candidate_limit", 60))],
            max_items,
            assistant_settings=assistant_settings,
        )
        raw_response = ""
        try:
            raw_response = ai_client.chat([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ])
            try:
                _write_debug_artifact("last-digest-raw.txt", raw_response)
            except Exception:
                pass
            parsed = _parse_json_response(raw_response)
            result = {
                "daily_title": parsed.get("daily_title") or f"{app_name} 24 小时速览",
                "brief": parsed.get("brief") or "",
                "themes": parsed.get("themes") or [],
                "items": [],
            }
            for raw_item in (parsed.get("items") or [])[:max_items]:
                merged = _attach_assets(raw_item, candidate_map)
                if not merged.get("image_url"):
                    merged["image_url"] = ""
                merged["summary"] = _truncate(merged.get("summary", ""), 500)
                merged["importance"] = float(merged.get("importance", 0) or 0)
                merged.setdefault("importance_reason", "")
                merged.setdefault("display_title", merged.get("title", ""))
                result["items"].append(merged)

            if not result["items"]:
                try:
                    _write_debug_artifact(
                        "last-digest-empty-items.txt",
                        f"build_daily_digest returned empty items on first pass\n\nraw_response:\n{raw_response}\n",
                    )
                except Exception:
                    pass
                result = _heuristic_digest(candidates, max_items, app_name=app_name, empty_hint=empty_hint)
        except Exception as exc:
            try:
                _write_debug_artifact(
                    "last-digest-error.txt",
                    f"build_daily_digest failed at first pass\n\nexception:\n{exc!r}\n\nraw_response:\n{raw_response}\n",
                )
            except Exception:
                pass
            result = _heuristic_digest(candidates, max_items, app_name=app_name, empty_hint=empty_hint)

    if not result.get("items"):
        refresh_info = _attempt_auto_refresh(assistant_settings)
        if refresh_info.get("success"):
            candidates = collect_candidates(lookback_hours=lookback_hours, assistant_settings=assistant_settings)
            candidate_map = {item.item_id: item for item in candidates}
            if api_key:
                try:
                    raw_response = ""
                    system_prompt, user_prompt = _build_prompt(
                        candidates[: int(assistant_settings.get("candidate_limit", 60))],
                        max_items,
                        assistant_settings=assistant_settings,
                    )
                    raw_response = ai_client.chat([
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ])
                    try:
                        _write_debug_artifact("last-digest-raw.txt", raw_response)
                    except Exception:
                        pass
                    parsed = _parse_json_response(raw_response)
                    result = {
                        "daily_title": parsed.get("daily_title") or f"{app_name} 24 小时速览",
                        "brief": parsed.get("brief") or "",
                        "themes": parsed.get("themes") or [],
                        "items": [],
                    }
                    for raw_item in (parsed.get("items") or [])[:max_items]:
                        merged = _attach_assets(raw_item, candidate_map)
                        if not merged.get("image_url"):
                            merged["image_url"] = ""
                        merged["summary"] = _truncate(merged.get("summary", ""), 500)
                        merged["importance"] = float(merged.get("importance", 0) or 0)
                        merged.setdefault("importance_reason", "")
                        merged.setdefault("display_title", merged.get("title", ""))
                        result["items"].append(merged)
                    if not result["items"]:
                        try:
                            _write_debug_artifact(
                                "last-digest-empty-items.txt",
                                f"build_daily_digest returned empty items after auto refresh\n\nraw_response:\n{raw_response}\n",
                            )
                        except Exception:
                            pass
                except Exception as exc:
                    try:
                        _write_debug_artifact(
                            "last-digest-error.txt",
                            f"build_daily_digest failed after auto refresh\n\nexception:\n{exc!r}\n\nraw_response:\n{raw_response}\n",
                        )
                    except Exception:
                        pass
                    result = _heuristic_digest(candidates, max_items, app_name=app_name, empty_hint=empty_hint)
            else:
                result = _heuristic_digest(candidates, max_items, app_name=app_name, empty_hint=empty_hint)
        else:
            empty_hint = f"{empty_hint} 已自动尝试补采，但暂未成功：{refresh_info.get('message', '')}".strip()
            result.setdefault("brief", empty_hint)

    # 后处理：补齐字段与排序
    normalized_items: List[Dict[str, Any]] = []
    for item in result.get("items", []):
        merged = _attach_assets(item, candidate_map)
        merged["summary"] = _truncate(merged.get("summary", ""), 500)
        merged.setdefault("importance", 0)
        merged.setdefault("importance_reason", "")
        merged.setdefault("source_type", "")
        merged.setdefault("published_at", "")
        merged.setdefault("display_title", merged.get("title", ""))
        merged.setdefault("original_title", merged.get("title", ""))
        merged["image_url"] = merged.get("image_url", "") or ""
        normalized_items.append(merged)

    normalized_items.sort(key=lambda x: float(x.get("importance", 0)), reverse=True)
    normalized_items = normalized_items[:max_items]

    if not result.get("themes"):
        result["themes"] = _derive_themes_from_candidates([candidate_map[item["item_id"]] for item in normalized_items if item.get("item_id") in candidate_map])

    if normalized_items and (
        not result.get("brief")
        or "请先运行采集任务" in str(result.get("brief", ""))
        or "暂无" in str(result.get("brief", ""))
    ):
        result["brief"] = f"已自动补采并生成 {len(normalized_items)} 条重点新闻。"

    report_id = _make_item_id(report_date, generated_at, result.get("daily_title", "digest"))
    report = {
        "report_id": report_id,
        "report_date": report_date,
        "generated_at": generated_at,
        "title": result.get("daily_title", f"{app_name} 24 小时速览"),
        "brief": result.get("brief", ""),
        "themes": result.get("themes", [])[:5],
        "window_start": window_start,
        "window_end": window_end,
        "items": normalized_items,
        "candidate_count": len(candidates),
        "source_summary": {
            "hotlist_count": sum(1 for item in candidates if item.source_type == "hotlist"),
            "rss_count": sum(1 for item in candidates if item.source_type == "rss"),
        },
        "assistant": {
            "lookback_hours": lookback_hours,
            "max_items": max_items,
            "generator": "volc-ark" if api_key else "heuristic",
            "auto_refresh_on_empty": bool(assistant_settings.get("auto_refresh_on_empty", True)),
            "auto_refresh_hint": empty_hint,
        },
        "app_name": app_name,
    }

    storage.save_report(report)
    _write_latest_report_files(report)
    return report


def _write_latest_report_files(report: Dict[str, Any]) -> None:
    output_dir = _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    latest_json = output_dir / "latest-report.json"
    latest_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path = output_dir / "latest-report.html"
    html_path.write_text(render_report_html(report), encoding="utf-8")


def _write_debug_artifact(name: str, content: str) -> None:
    output_dir = _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_text(content, encoding="utf-8")


def render_report_html(report: Dict[str, Any]) -> str:
    items = report.get("items", [])
    bookmarks = report.get("bookmarks", [])
    logs = report.get("logs", [])
    app_name = html.escape(report.get("app_name", "DAY VIBE AI"))
    empty_hint = html.escape(
        report.get("assistant", {}).get(
            "auto_refresh_hint",
            "当前没有可用候选新闻，请先运行采集任务。",
        )
    )

    def display_title(item: Dict[str, Any]) -> str:
        return item.get("display_title") or item.get("title") or ""

    def escaped_display_title(item: Dict[str, Any]) -> str:
        return html.escape(display_title(item))

    def escaped_original_title(item: Dict[str, Any]) -> str:
        return html.escape(item.get("original_title") or item.get("title") or "")

    def source_stamp(item: Dict[str, Any]) -> str:
        source = html.escape(item.get("source_name", ""))
        published = html.escape(item.get("published_at", ""))
        return f"{source} · {published}" if published else source

    hero_item = items[0] if items else {}
    hero_image = html.escape(hero_item.get("image_url") or "")
    hero_display = escaped_display_title(hero_item) if hero_item else "点击一条新闻开始浏览"
    hero_original = escaped_original_title(hero_item) if hero_item else ""
    hero_source = source_stamp(hero_item) if hero_item else ""
    hero_summary = html.escape(hero_item.get("summary", "")) if hero_item else ""

    themes = report.get("themes", []) or []
    themes_html = "".join(
        f'<span class="signal-chip theme-pill">{html.escape(theme)}</span>'
        for theme in themes
    ) or '<span class="signal-chip theme-pill">AI / 科技</span>'

    item_cards = []
    for idx, item in enumerate(items, start=1):
        cover = (
            f'<img src="{html.escape(item.get("image_url") or "")}" alt="">'
            if item.get("image_url")
            else f'<div class="thumb-empty">{html.escape(item.get("source_name") or "暂无配图")}</div>'
        )
        item_cards.append(
            f"""
            <article class="story-row" data-item-id="{html.escape(item['item_id'])}">
              <div class="story-visual">{cover}</div>
              <div class="story-copy">
                <div class="story-meta compact">
                  <span class="story-rank">#{idx}</span>
                  <span>{html.escape(item.get('source_name', ''))}</span>
                  <span>{html.escape(item.get('published_at', ''))}</span>
                  <span>重要性 {html.escape(str(int(float(item.get('importance', 0) or 0))))}</span>
                </div>
                <h3>{escaped_display_title(item)}</h3>
                <p>{html.escape(item.get('summary', ''))}</p>
                <div class="story-actions actions">
                  <button class="primary" data-action="bookmark">收藏</button>
                  <button data-action="log">写阅读日志</button>
                  <a href="{html.escape(item.get('source_url', '#'))}" target="_blank" rel="noreferrer">原文</a>
                </div>
              </div>
            </article>
            """
        )

    cards_html = (
        f'<div class="story-list">{"".join(item_cards)}</div>'
        if items
        else f'<div class="empty">{empty_hint}<br><br>系统已在空日报时尝试自动补采；如果仍为空，请检查采集任务和数据源连通性。</div>'
    )

    bookmarks_html = "".join(
        f"""
        <article class="collection-card bookmark-card" data-bookmark-id="{html.escape(bookmark.get('item_id', ''))}">
          <div class="collection-visual">
            {f'<img src="{html.escape(bookmark.get("image_url") or "")}" alt="">' if bookmark.get("image_url") else '<div class="thumb-empty" style="min-height:88px;border-radius:18px;">暂无配图</div>'}
          </div>
          <div class="collection-copy">
            <h4>{html.escape(bookmark.get('display_title') or bookmark.get('title', ''))}</h4>
            <p>{html.escape(bookmark.get('source_name', ''))}</p>
            <span>{html.escape(bookmark.get('updated_at', bookmark.get('created_at', '')))}</span>
          </div>
        </article>
        """
        for bookmark in bookmarks[:6]
    )

    logs_html = "".join(
        f"""
        <article class="journal-card log-card" data-log-item-id="{html.escape(log.get('item_id', ''))}">
          <div class="journal-topline">
            <span>阅读日志</span>
            <span>{html.escape(log.get('created_at', ''))}</span>
          </div>
          <h4>{html.escape(log.get('display_title') or log.get('log_title') or log.get('draft_title') or log.get('title') or '')}</h4>
          <p>{html.escape(_truncate(log.get('log_text', ''), 140))}</p>
        </article>
        """
        for log in logs[:6]
    )

    title = html.escape(report.get("title", f"{app_name} 24 小时速览"))
    brief = html.escape(report.get("brief", ""))
    generated_at = html.escape(report.get("generated_at", ""))
    report_json = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title}</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Inter:wght@400;500;600;700&display=swap');

    :root {{
      --bg: #060912;
      --bg-elevated: rgba(12, 17, 33, 0.82);
      --panel: rgba(11, 17, 31, 0.82);
      --panel-strong: rgba(15, 21, 39, 0.94);
      --card: rgba(255, 255, 255, 0.05);
      --card-strong: rgba(255, 255, 255, 0.07);
      --line: rgba(255, 255, 255, 0.09);
      --line-strong: rgba(255, 255, 255, 0.16);
      --text: #edf3ff;
      --muted: #90a0bb;
      --soft: #c9d7f2;
      --accent: #8ff5ff;
      --accent-strong: #5ba0ff;
      --accent-2: #b38cff;
      --danger: #ff756f;
      --shadow: 0 28px 90px rgba(0, 0, 0, 0.42);
      --radius-xl: 34px;
      --radius-lg: 24px;
      --radius-md: 18px;
      --radius-sm: 14px;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: "Inter", ui-sans-serif, system-ui, sans-serif;
      background:
        radial-gradient(circle at 14% 16%, rgba(91, 160, 255, 0.24), transparent 24%),
        radial-gradient(circle at 88% 14%, rgba(179, 140, 255, 0.18), transparent 22%),
        radial-gradient(circle at 62% 52%, rgba(143, 245, 255, 0.08), transparent 24%),
        linear-gradient(180deg, #05070e 0%, #09111e 58%, #071019 100%);
      overflow-x: hidden;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: 0.22;
      background-image:
        linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
      background-size: 72px 72px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,.8), rgba(0,0,0,.1));
    }}
    a {{
      color: inherit;
      text-decoration: none;
    }}
    button, input, textarea {{
      font: inherit;
    }}
    button {{
      cursor: pointer;
    }}
    .shell {{
      max-width: 1480px;
      margin: 0 auto;
      padding: 24px 22px 64px;
      position: relative;
      z-index: 1;
    }}
    .masthead {{
      position: sticky;
      top: 14px;
      z-index: 30;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 14px 18px;
      margin-bottom: 20px;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 999px;
      background: rgba(7, 11, 21, 0.72);
      backdrop-filter: blur(18px);
      box-shadow: 0 8px 30px rgba(0,0,0,.28);
    }}
    .brandline {{
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }}
    .brand-glyph {{
      width: 40px;
      height: 40px;
      border-radius: 14px;
      border: 1px solid rgba(143,245,255,.24);
      background:
        linear-gradient(135deg, rgba(143,245,255,.22), rgba(91,160,255,.08)),
        rgba(255,255,255,.03);
      box-shadow: inset 0 0 40px rgba(143,245,255,.08);
      position: relative;
      overflow: hidden;
    }}
    .brand-glyph::after {{
      content: "";
      position: absolute;
      inset: 8px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,.14);
    }}
    .brand-copy {{
      min-width: 0;
    }}
    .brand-name {{
      font-family: "Space Grotesk", sans-serif;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: .22em;
      text-transform: uppercase;
      color: var(--accent);
    }}
    .brand-sub {{
      margin-top: 2px;
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .masthead-nav {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .nav-pill {{
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      color: var(--soft);
      font-size: 12px;
    }}
    .nav-pill.button {{
      cursor: pointer;
      font: inherit;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(340px, 420px);
      gap: 22px;
      align-items: start;
    }}
    .main {{
      min-width: 0;
      display: grid;
      gap: 22px;
    }}
    .side {{
      position: sticky;
      top: 96px;
      min-width: 0;
    }}
    .hero {{
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: var(--radius-xl);
      background:
        linear-gradient(145deg, rgba(8, 13, 26, 0.92), rgba(10, 17, 34, 0.72)),
        radial-gradient(circle at top right, rgba(143,245,255,.12), transparent 28%);
      box-shadow: var(--shadow);
      padding: 22px;
    }}
    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(110deg, rgba(143,245,255,.08), transparent 24%),
        linear-gradient(300deg, rgba(179,140,255,.08), transparent 28%);
      pointer-events: none;
    }}
    .hero-grid {{
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(240px, .8fr);
      gap: 18px;
      align-items: stretch;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--accent);
      font-family: "Space Grotesk", sans-serif;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .28em;
      text-transform: uppercase;
    }}
    .eyebrow::before {{
      content: "";
      width: 30px;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(143,245,255,.9));
    }}
    .hero-title {{
      margin: 16px 0 12px;
      max-width: 13ch;
      font-family: "Space Grotesk", sans-serif;
      font-size: clamp(28px, 3.9vw, 46px);
      line-height: 1.03;
      letter-spacing: -.045em;
      text-wrap: balance;
    }}
    .hero-brief {{
      max-width: 60ch;
      font-size: 14px;
      line-height: 1.76;
      color: var(--soft);
    }}
    .hero-stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .stat-chip, .signal-chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      padding: 9px 13px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      color: var(--soft);
      font-size: 11px;
    }}
    .signal-chip {{
      backdrop-filter: blur(8px);
    }}
    .theme-pill {{
      color: #d7f4ff;
      background: linear-gradient(135deg, rgba(143,245,255,.12), rgba(91,160,255,.08));
    }}
    .hero-actions, .toolbar, .actions, .detail-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .hero-actions {{
      margin-top: 16px;
    }}
    .hero-actions button,
    .hero-actions a,
    .toolbar button,
    .toolbar a,
    .actions button,
    .actions a,
    .detail-actions button,
    .detail-actions a,
    dialog button {{
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(255,255,255,0.04);
      color: var(--text);
      padding: 11px 16px;
      border-radius: 999px;
      transition: transform .24s ease, border-color .24s ease, background .24s ease;
    }}
    .hero-actions button:hover,
    .hero-actions a:hover,
    .toolbar button:hover,
    .toolbar a:hover,
    .actions button:hover,
    .actions a:hover,
    .detail-actions button:hover,
    .detail-actions a:hover,
    dialog button:hover {{
      transform: translateY(-1px);
      border-color: rgba(143,245,255,.34);
      background: rgba(143,245,255,.08);
    }}
    .hero-actions .primary,
    .actions .primary,
    .detail-actions .primary {{
      background: linear-gradient(135deg, rgba(143,245,255,.16), rgba(91,160,255,.18));
      border-color: rgba(143,245,255,.26);
      color: #effcff;
    }}
    .refresh-status {{
      min-height: 22px;
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    .hero-feature {{
      display: grid;
      grid-template-rows: minmax(240px, 1fr) auto;
      overflow: hidden;
      border-radius: 26px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
      min-height: 100%;
    }}
    .hero-feature-visual {{
      position: relative;
      min-height: 240px;
      overflow: hidden;
      background: linear-gradient(135deg, rgba(91,160,255,.18), rgba(179,140,255,.12));
    }}
    .hero-feature-visual img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
      filter: saturate(1.05) contrast(1.02);
    }}
    .hero-feature-copy {{
      padding: 16px 16px 18px;
      background: linear-gradient(180deg, rgba(14,20,36,.86), rgba(14,20,36,.96));
    }}
    .hero-feature-copy h2 {{
      margin: 8px 0 6px;
      font-size: 17px;
      line-height: 1.24;
      letter-spacing: -.03em;
    }}
    .hero-feature-copy p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.72;
      font-size: 12px;
    }}
    .story-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .story-meta.compact {{
      font-size: 11px;
      letter-spacing: .06em;
    }}
    .story-rank {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(255,255,255,.06);
      color: var(--accent);
      font-weight: 700;
      letter-spacing: .04em;
    }}
    .section-shell {{
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: var(--radius-xl);
      background: linear-gradient(180deg, rgba(10, 15, 29, 0.84), rgba(8, 13, 24, 0.72));
      box-shadow: var(--shadow);
      padding: 20px;
      scroll-margin-top: 96px;
    }}
    .section-head {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 14px;
    }}
    .section-title {{
      margin: 0;
      font-family: "Space Grotesk", sans-serif;
      font-size: 18px;
      line-height: 1;
      letter-spacing: -.03em;
    }}
    .section-note {{
      color: var(--muted);
      font-size: 11px;
      max-width: 48ch;
    }}
    .section-shell .story-list {{
      display: grid;
      gap: 12px;
    }}
    .story-row,
    .collection-card,
    .journal-card {{
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 22px;
      background: rgba(255,255,255,.03);
      transition: transform .24s ease, border-color .24s ease, background .24s ease;
    }}
    .story-row:hover,
    .collection-card:hover,
    .journal-card:hover {{
      transform: translateY(-2px);
      border-color: rgba(143,245,255,.2);
      background: rgba(255,255,255,.05);
    }}
    .story-row {{
      display: grid;
      grid-template-columns: 160px minmax(0, 1fr);
      gap: 16px;
      overflow: hidden;
      min-height: 160px;
      padding: 0;
      cursor: pointer;
    }}
    .story-visual {{
      position: relative;
      min-height: 160px;
      background: linear-gradient(145deg, rgba(91,160,255,.16), rgba(179,140,255,.14));
      overflow: hidden;
    }}
    .story-visual img,
    .collection-visual img,
    .detail-cover img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .story-copy {{
      padding: 16px 16px 16px 0;
      display: flex;
      flex-direction: column;
      justify-content: end;
      gap: 10px;
    }}
    .story-copy h3,
    .collection-copy h4,
    .journal-card h4,
    .detail-title {{
      margin: 0;
      font-family: "Space Grotesk", sans-serif;
      letter-spacing: -.03em;
      text-wrap: balance;
    }}
    .story-copy h3 {{
      font-size: 17px;
      line-height: 1.22;
    }}
    .story-copy p {{
      margin: 0;
      color: var(--soft);
      font-size: 12px;
      line-height: 1.72;
    }}
    .story-actions {{ margin-top: 2px; }}
    .collections-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}
    .section-card {{
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 26px;
      background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
      padding: 18px;
      min-width: 0;
      scroll-margin-top: 96px;
    }}
    .collection-list,
    .journal-list,
    .small-list {{
      display: grid;
      gap: 12px;
    }}
    .collection-card {{
      display: grid;
      grid-template-columns: 110px minmax(0, 1fr);
      gap: 14px;
      padding: 12px;
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,0.06);
      background: rgba(255,255,255,.03);
      cursor: pointer;
      transition: transform .22s ease, border-color .22s ease;
    }}
    .collection-visual {{
      min-height: 88px;
      overflow: hidden;
      border-radius: 18px;
      background: linear-gradient(135deg, rgba(91,160,255,.15), rgba(179,140,255,.12));
    }}
    .collection-copy {{
      display: grid;
      gap: 6px;
      align-content: center;
      min-width: 0;
    }}
    .collection-copy h4 {{
      font-size: 14px;
      line-height: 1.22;
    }}
    .collection-copy p,
    .collection-copy span,
    .journal-topline {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .journal-card {{
      padding: 16px 18px;
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,0.06);
      background: rgba(255,255,255,.03);
      cursor: pointer;
      transition: transform .22s ease, border-color .22s ease;
    }}
    .journal-topline {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      text-transform: uppercase;
      letter-spacing: .1em;
    }}
    .journal-card h4 {{
      margin-top: 12px;
      font-size: 16px;
      line-height: 1.22;
    }}
    .journal-card p,
    .log-preview {{
      margin-top: 10px;
      color: var(--soft);
      font-size: 13px;
      line-height: 1.7;
    }}
    .empty {{
      padding: 26px;
      border: 1px dashed rgba(255,255,255,.14);
      border-radius: 24px;
      background: rgba(255,255,255,.03);
      color: var(--muted);
      line-height: 1.8;
    }}
    .detail-panel {{
      overflow: hidden;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 28px;
      background:
        linear-gradient(180deg, rgba(8, 13, 24, 0.92), rgba(8, 13, 24, 0.84)),
        radial-gradient(circle at top right, rgba(143,245,255,.08), transparent 30%);
      box-shadow: var(--shadow);
    }}
    .detail-cover {{
      position: relative;
      min-height: 200px;
      background: linear-gradient(135deg, rgba(91,160,255,.18), rgba(179,140,255,.12));
      overflow: hidden;
    }}
    .detail-cover::after {{
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(180deg, rgba(8,13,24,0) 0%, rgba(8,13,24,.72) 100%);
      pointer-events: none;
    }}
    .thumb-empty {{
      width: 100%;
      height: 100%;
      min-height: 120px;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      text-align: center;
      color: rgba(237,243,255,.72);
      font-size: 14px;
      line-height: 1.6;
      background: linear-gradient(135deg, rgba(16,25,46,.94), rgba(43,78,167,.64));
    }}
    .detail-body {{
      padding: 16px;
      display: grid;
      gap: 12px;
    }}
    .detail-kicker {{
      color: var(--accent);
      font-family: "Space Grotesk", sans-serif;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .22em;
      text-transform: uppercase;
    }}
    .detail-title {{
      font-size: 19px;
      line-height: 1.2;
    }}
    .detail-original {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
      white-space: pre-wrap;
    }}
    .detail-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .detail-pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.03);
    }}
    .detail-section {{
      min-width: 0;
      display: grid;
      gap: 8px;
      padding-top: 12px;
      border-top: 1px solid rgba(255,255,255,.06);
    }}
    .detail-section h3 {{
      margin: 0;
      color: var(--soft);
      font-family: "Space Grotesk", sans-serif;
      font-size: 15px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    .detail-text {{
      color: var(--soft);
      line-height: 1.74;
      font-size: 12px;
      white-space: pre-wrap;
    }}
    .detail-note-card {{
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.02);
      padding: 14px;
    }}
    .logbox,
    .field-input {{
      width: 100%;
      max-width: 100%;
      box-sizing: border-box;
      display: block;
      border: 1px solid rgba(255,255,255,.08);
      border-radius: 18px;
      padding: 16px;
      background: rgba(6, 9, 18, 0.72);
      color: var(--text);
      outline: none;
      transition: border-color .22s ease, box-shadow .22s ease, background .22s ease;
    }}
    .field-input {{
      min-height: 52px;
    }}
    .logbox {{
      min-height: 120px;
      resize: vertical;
      line-height: 1.8;
    }}
    .field-input:focus,
    .logbox:focus {{
      border-color: rgba(143,245,255,.34);
      box-shadow: 0 0 0 3px rgba(143,245,255,.1);
      background: rgba(9, 14, 27, 0.86);
    }}
    .field-label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .12em;
      margin-bottom: 10px;
    }}
    dialog {{
      width: min(980px, 92vw);
      border: 1px solid rgba(255,255,255,.1);
      border-radius: 30px;
      padding: 0;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(10, 16, 31, 0.98), rgba(7, 11, 21, 0.98)),
        radial-gradient(circle at top right, rgba(143,245,255,.1), transparent 24%);
      color: var(--text);
      box-shadow: 0 40px 120px rgba(0, 0, 0, 0.55);
    }}
    dialog::backdrop {{
      background: rgba(2, 7, 15, .74);
      backdrop-filter: blur(12px);
    }}
    dialog form {{
      display: grid;
      gap: 0;
    }}
    .log-shell {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(200px, 260px);
      min-height: 460px;
    }}
    .log-main {{
      padding: 20px;
      display: grid;
      gap: 12px;
    }}
    .log-side {{
      border-left: 1px solid rgba(255,255,255,.06);
      padding: 20px 18px;
      background: rgba(255,255,255,.02);
      display: grid;
      align-content: start;
      gap: 12px;
    }}
    .log-heading {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 14px;
      padding-bottom: 16px;
      border-bottom: 1px solid rgba(255,255,255,.06);
    }}
    .log-heading strong {{
      font-family: "Space Grotesk", sans-serif;
      font-size: 18px;
      line-height: 1;
      letter-spacing: -.03em;
    }}
    .log-subtitle {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
    }}
    .log-meta-card {{
      border-radius: 20px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.03);
      padding: 14px 16px;
    }}
    .log-meta-card h4 {{
      margin: 0 0 8px;
      font-family: "Space Grotesk", sans-serif;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--soft);
    }}
    .log-meta-card p,
    .log-meta-card div {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
    }}
    .modal-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 8px;
    }}
    .small-item {{
      padding: 14px 16px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.02);
    }}
    @keyframes riseIn {{
      from {{
        opacity: 0;
        transform: translateY(14px);
      }}
      to {{
        opacity: 1;
        transform: translateY(0);
      }}
    }}
    .hero,
    .section-shell,
    .detail-panel {{
      animation: riseIn .5s ease both;
    }}
    @media (max-width: 1180px) {{
      .layout {{
        grid-template-columns: 1fr;
      }}
      .side {{
        position: static;
      }}
      .hero-grid,
      .collections-grid,
      .log-shell,
      .story-row {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 820px) {{
      .shell {{
        padding: 16px 14px 56px;
      }}
      .masthead {{
        border-radius: 24px;
        align-items: flex-start;
        flex-direction: column;
      }}
      .masthead-nav {{
        justify-content: flex-start;
      }}
      .hero {{
        padding: 20px;
      }}
      .hero-title {{
        max-width: none;
      }}
      .hero-feature {{
        min-height: 0;
      }}
      .section-shell {{
        padding: 18px;
      }}
      .detail-body,
      .log-main,
      .log-side {{
        padding: 18px;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <header class="masthead">
      <div class="brandline">
        <div class="brand-glyph"></div>
        <div class="brand-copy">
          <div class="brand-name">{app_name}</div>
          <div class="brand-sub">AI 行业日志助手 · 每日情报、收藏与阅读沉淀</div>
        </div>
      </div>
      <div class="masthead-nav">
        <span class="nav-pill">日报主页</span>
        <button class="nav-pill button" type="button" onclick="scrollToSection('bookmarkSection')">收藏列表</button>
        <button class="nav-pill button" type="button" onclick="scrollToSection('logSection')">阅读日志</button>
      </div>
    </header>

    <div class="layout">
      <main class="main">
        <section class="hero">
          <div class="hero-grid">
            <div>
              <div class="eyebrow">{app_name}</div>
              <h1 id="heroTitle" class="hero-title">{title}</h1>
              <div id="heroBrief" class="hero-brief">{brief}</div>
              <div class="hero-stats">
                <span id="heroGeneratedAt" class="stat-chip">生成时间: {generated_at}</span>
                <span id="heroCount" class="stat-chip">选中: {len(items)}</span>
              </div>
              <div id="heroThemes" class="hero-stats">{themes_html}</div>
              <div class="hero-actions">
                <button id="refreshBtn" class="primary" onclick="refreshDigest()">刷新日报</button>
                <button onclick="document.getElementById('cards').scrollIntoView({{behavior:'smooth'}})">浏览重点</button>
                <button type="button" onclick="scrollToSection('logSection')">查看日志</button>
              </div>
              <div id="refreshStatus" class="refresh-status" aria-live="polite"></div>
            </div>

            <div class="hero-feature" id="heroFeature">
              <div class="hero-feature-visual">
                {f'<img src="{hero_image}" alt="">' if hero_image else '<div class="thumb-empty">暂无配图，待后续抓取</div>'}
              </div>
              <div class="hero-feature-copy">
                <div class="story-meta">
                  <span class="story-rank">TOP</span>
                  <span>{hero_source}</span>
                </div>
                <h2 id="heroLeadTitle">{hero_display}</h2>
                <p id="heroLeadSummary">{hero_summary}</p>
                <div id="heroLeadOriginal" class="detail-original">{hero_original if hero_original and hero_original != hero_display else ""}</div>
              </div>
            </div>
          </div>
        </section>

        <section class="section-shell" id="newsSection">
          <div class="section-head">
            <div>
              <h2 class="section-title">今日重点</h2>
              <div class="section-note">按重要性排序的前沿 AI / 科技 信号流，保留你每天最该看的那几条。</div>
            </div>
          </div>
          <div id="cards">
            {cards_html}
          </div>
        </section>

        <section class="collections-grid">
          <div class="section-card" id="bookmarkSection">
            <div class="section-head">
              <div>
                <h2 class="section-title">收藏</h2>
                <div class="section-note">保留下来，稍后写注释、沉淀观点。</div>
              </div>
            </div>
            <div id="bookmarkList" class="collection-list">
              {bookmarks_html if bookmarks_html else '<div class="empty">暂无收藏。点击任意新闻的“收藏”按钮即可加入这里。</div>'}
            </div>
          </div>

          <div class="section-card" id="logSection">
            <div class="section-head">
              <div>
                <h2 class="section-title">阅读日志</h2>
                <div class="section-note">让模型先写一版，再改成你自己的判断。</div>
              </div>
            </div>
            <div id="logList" class="journal-list">
              {logs_html if logs_html else '<div class="empty">还没有阅读日志。选一条新闻，先让模型生成初稿，再改成你的版本。</div>'}
            </div>
          </div>
        </section>
      </main>

      <aside class="side">
        <div class="detail-panel" id="detailPanel">
          <div class="detail-cover">
            <div class="thumb-empty">选中一条新闻后，这里会显示更完整的摘要、原始标题、收藏备注和阅读日志历史。</div>
          </div>
          <div class="detail-body">
            <div class="detail-kicker">Inspector</div>
            <div class="detail-title">点击一条新闻查看详情</div>
            <div class="detail-text">当前页面保留了日报、收藏和阅读日志的完整闭环，你可以在这里快速完成浏览、收藏、写日志和打开原文。</div>
          </div>
        </div>
      </aside>
    </div>
  </div>

  <dialog id="logDialog">
    <form method="dialog" id="logForm">
      <div class="log-shell">
        <div class="log-main">
          <div class="log-heading">
            <div>
              <strong>写阅读日志</strong>
              <div class="log-subtitle">先由模型生成一版完整初稿，再改成你自己的版本。</div>
            </div>
            <button value="cancel">关闭</button>
          </div>
          <input type="hidden" id="logItemId">
          <input type="hidden" id="logDraftText">
          <div>
            <label class="field-label" for="logTitle">日志标题</label>
            <input id="logTitle" class="field-input" type="text" placeholder="这条阅读记录的标题">
          </div>
          <div>
            <label class="field-label" for="logText">正文</label>
            <textarea id="logText" class="logbox" rows="10" placeholder="写下你的阅读日志..."></textarea>
          </div>
          <div class="modal-actions">
            <button type="button" onclick="generateLogDraft()">重新生成初稿</button>
            <button class="primary" value="default" onclick="submitLog(event)">保存日志</button>
          </div>
        </div>
        <aside class="log-side">
          <div class="log-meta-card">
            <h4>生成状态</h4>
            <div id="logDraftMeta">等待生成阅读日志初稿...</div>
          </div>
          <div class="log-meta-card">
            <h4>写作提示</h4>
            <p>保留事实、加上判断，再写出你接下来准备继续跟踪什么。</p>
          </div>
          <div class="log-meta-card">
            <h4>编辑原则</h4>
            <p>像在写一篇短小但有判断力的科技观察，而不是简单复述新闻。</p>
          </div>
        </aside>
      </div>
    </form>
  </dialog>

  <script>
    const REPORT_DATA = {report_json};
    let currentDetailItemId = null;
    let refreshCountdownTimer = null;
    let refreshCountdownRemaining = 0;

    function escapeHtml(value) {{
      return String(value || '').replace(/[&<>"']/g, (ch) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    }}

    function scrollToSection(sectionId) {{
      const section = document.getElementById(sectionId);
      if (!section) return;
      section.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }}

    function getDisplayTitle(item) {{
      return item?.display_title || item?.title || '';
    }}

    function getOriginalTitle(item) {{
      return item?.original_title || item?.title || '';
    }}

    function findItemById(itemId) {{
      return (REPORT_DATA.items || []).find((item) => item.item_id === itemId)
        || (REPORT_DATA.bookmarks || []).find((item) => item.item_id === itemId)
        || null;
    }}

    function getLogsForItem(itemId) {{
      return (REPORT_DATA.logs || []).filter((log) => log.item_id === itemId);
    }}

    function getBookmarkForItem(itemId) {{
      return (REPORT_DATA.bookmarks || []).find((bookmark) => bookmark.item_id === itemId) || null;
    }}

    function formatCountdown(seconds) {{
      const safeSeconds = Math.max(0, Math.floor(seconds || 0));
      const minutes = Math.floor(safeSeconds / 60);
      const remainder = safeSeconds % 60;
      return `${{minutes}}:${{String(remainder).padStart(2, '0')}}`;
    }}

    function setRefreshLoading(loading, message = '') {{
      const btn = document.getElementById('refreshBtn');
      const status = document.getElementById('refreshStatus');
      if (refreshCountdownTimer) {{
        clearInterval(refreshCountdownTimer);
        refreshCountdownTimer = null;
      }}
      if (btn) {{
        btn.disabled = loading;
        btn.style.opacity = loading ? '0.72' : '1';
        btn.textContent = loading ? '刷新中...' : '刷新日报';
      }}
      if (status && message) {{
        status.textContent = message;
      }}
      if (!loading && status && !message) {{
        status.textContent = '';
      }}
    }}

    function startRefreshCountdown(estimateSeconds = 180) {{
      refreshCountdownRemaining = estimateSeconds;
      const status = document.getElementById('refreshStatus');
      if (status) {{
        status.textContent = `正在重新抓取并生成日报，预计剩余 ${{formatCountdown(refreshCountdownRemaining)}}。`;
      }}
      refreshCountdownTimer = setInterval(() => {{
        refreshCountdownRemaining -= 1;
        if (refreshCountdownRemaining <= 0) {{
          if (status) {{
            status.textContent = '正在重新抓取并生成日报，已接近完成，请稍候...';
          }}
          clearInterval(refreshCountdownTimer);
          refreshCountdownTimer = null;
          return;
        }}
        if (status) {{
          status.textContent = `正在重新抓取并生成日报，预计剩余 ${{formatCountdown(refreshCountdownRemaining)}}。`;
        }}
      }}, 1000);
    }}

    function renderHero(report) {{
      const first = (report.items || [])[0] || null;
      document.getElementById('heroTitle').textContent = report.title || '';
      document.getElementById('heroBrief').textContent = report.brief || '';
      document.getElementById('heroGeneratedAt').textContent = `生成时间: ${{report.generated_at || ''}}`;
      document.getElementById('heroCount').textContent = `选中: ${{(report.items || []).length}}`;
      document.getElementById('heroThemes').innerHTML = (report.themes || []).length
        ? (report.themes || []).map((theme) => `<span class="signal-chip theme-pill">${{escapeHtml(theme)}}</span>`).join('')
        : '<span class="signal-chip theme-pill">AI / 科技</span>';

      const feature = document.getElementById('heroFeature');
      if (!first) {{
        feature.innerHTML = `
          <div class="hero-feature-visual"><div class="thumb-empty">暂无重点新闻，稍后再刷新试试</div></div>
          <div class="hero-feature-copy">
            <div class="story-meta"><span class="story-rank">TOP</span><span>等待数据</span></div>
            <h2 id="heroLeadTitle">今天还没有新的重点新闻</h2>
            <p id="heroLeadSummary">系统会继续在下一次刷新时抓取最新内容。</p>
            <div id="heroLeadOriginal" class="detail-original"></div>
          </div>
        `;
        return;
      }}
      feature.innerHTML = `
        <div class="hero-feature-visual">${{first.image_url ? `<img src="${{first.image_url}}" alt="">` : `<div class="thumb-empty">${{escapeHtml(first.source_name || '暂无配图，待后续抓取')}}</div>`}}</div>
        <div class="hero-feature-copy">
          <div class="story-meta">
            <span class="story-rank">TOP</span>
            <span>${{escapeHtml(first.source_name || '')}}</span>
            <span>${{escapeHtml(first.published_at || '')}}</span>
          </div>
          <h2 id="heroLeadTitle">${{escapeHtml(getDisplayTitle(first))}}</h2>
          <p id="heroLeadSummary">${{escapeHtml(first.summary || '')}}</p>
          <div id="heroLeadOriginal" class="detail-original">${{getOriginalTitle(first) !== getDisplayTitle(first) ? escapeHtml(getOriginalTitle(first)) : ''}}</div>
        </div>
      `;
    }}

    function renderCards(items) {{
      const root = document.getElementById('cards');
      if (!items.length) {{
        root.innerHTML = '<div class="empty">{empty_hint}<br><br>系统会先自动补采一次，再重新生成日报。</div>';
        return;
      }}

      const cards = items.map((item, index) => `
        <article class="story-row" data-item-id="${{item.item_id}}">
          <div class="story-visual">${{item.image_url ? `<img src="${{item.image_url}}" alt="">` : `<div class="thumb-empty">${{escapeHtml(item.source_name || '暂无配图，待后续抓取')}}</div>`}}</div>
          <div class="story-copy">
            <div class="story-meta">
              <span class="story-rank">#${{index + 1}}</span>
              <span>${{escapeHtml(item.source_name || '')}}</span>
              <span>${{escapeHtml(item.published_at || '')}}</span>
              <span>重要性 ${{escapeHtml(String(Math.round(item.importance || 0)))}}</span>
            </div>
            <h3>${{escapeHtml(getDisplayTitle(item))}}</h3>
            <p>${{escapeHtml(item.summary || '')}}</p>
            <div class="story-actions actions">
              <button class="primary" data-action="bookmark">收藏</button>
              <button data-action="log">写阅读日志</button>
              <a href="${{item.source_url || '#'}}" target="_blank" rel="noreferrer">原文</a>
            </div>
          </div>
        </article>
      `).join('');

      root.innerHTML = `<div class="story-list">${{cards}}</div>`;
    }}

    function renderBookmarkList(bookmarks) {{
      const root = document.getElementById('bookmarkList');
      if (!bookmarks.length) {{
        root.innerHTML = '<div class="empty">暂无收藏。点击任意新闻的“收藏”按钮即可加入这里。</div>';
        return;
      }}
      root.innerHTML = bookmarks.map((bookmark) => `
        <article class="collection-card bookmark-card" data-bookmark-id="${{bookmark.item_id}}">
          <div class="collection-visual">${{bookmark.image_url ? `<img src="${{bookmark.image_url}}" alt="">` : '<div class="thumb-empty" style="min-height:88px;border-radius:18px;">暂无配图</div>'}}</div>
          <div class="collection-copy">
            <h4>${{escapeHtml(bookmark.display_title || bookmark.title || '')}}</h4>
            <p>${{escapeHtml(bookmark.source_name || '')}}</p>
            <span>${{escapeHtml(bookmark.updated_at || bookmark.created_at || '')}}</span>
          </div>
        </article>
      `).join('');
      document.querySelectorAll('.bookmark-card').forEach((card) => {{
        card.onclick = () => openDetail(card.dataset.bookmarkId);
      }});
    }}

    function renderLogList(logs) {{
      const root = document.getElementById('logList');
      if (!logs.length) {{
        root.innerHTML = '<div class="empty">还没有阅读日志。选一条新闻，先让模型生成初稿，再改成你的版本。</div>';
        return;
      }}
      root.innerHTML = logs.map((log) => `
        <article class="journal-card log-card" data-log-item-id="${{log.item_id}}">
          <div class="journal-topline">
            <span>阅读日志</span>
            <span>${{escapeHtml(log.created_at || '')}}</span>
          </div>
          <h4>${{escapeHtml(log.display_title || log.log_title || log.draft_title || log.title || '')}}</h4>
          <p class="log-preview">${{escapeHtml((log.log_text || '').slice(0, 140))}}</p>
        </article>
      `).join('');
      document.querySelectorAll('.log-card').forEach((card) => {{
        card.onclick = () => openDetail(card.dataset.logItemId);
      }});
    }}

    async function loadLatest() {{
      const res = await fetch('/api/latest');
      const data = await res.json();
      if (!data.ok) {{
        alert(data.error || '加载失败');
        return;
      }}
      REPORT_DATA.items = data.report.items || [];
      REPORT_DATA.bookmarks = data.report.bookmarks || [];
      REPORT_DATA.logs = data.report.logs || [];
      REPORT_DATA.themes = data.report.themes || [];
      REPORT_DATA.brief = data.report.brief || '';
      REPORT_DATA.title = data.report.title || '';
      REPORT_DATA.generated_at = data.report.generated_at || '';
      REPORT_DATA.report_id = data.report.report_id || REPORT_DATA.report_id;
      renderReport(data.report);
    }}

    function renderReport(report) {{
      renderHero(report);
      renderCards(report.items || []);
      renderBookmarkList(report.bookmarks || []);
      renderLogList(report.logs || []);
      attachHandlers();
    }}

    function openDetail(itemId) {{
      const item = findItemById(itemId);
      if (!item) return;
      currentDetailItemId = itemId;
      const bookmark = getBookmarkForItem(itemId);
      const logs = getLogsForItem(itemId);
      const detail = document.getElementById('detailPanel');
      const bookmarked = Boolean(bookmark);
      const original = getOriginalTitle(item);
      const display = getDisplayTitle(item);

      detail.innerHTML = `
        <div class="detail-cover">${{item.image_url ? `<img src="${{item.image_url}}" alt="">` : `<div class="thumb-empty">${{escapeHtml(item.source_name || '暂无配图，待后续抓取')}}</div>`}}</div>
        <div class="detail-body">
          <div class="detail-kicker">Inspector</div>
          <div class="detail-title">${{escapeHtml(display)}}</div>
          <div class="detail-meta">
            <span class="detail-pill">${{escapeHtml(item.source_name || '')}}</span>
            <span class="detail-pill">${{escapeHtml(item.published_at || '')}}</span>
            <span class="detail-pill">重要性 ${{escapeHtml(String(Math.round(item.importance || 0)))}}</span>
          </div>
          ${{original && original !== display ? `<div class="detail-original">原始标题：${{escapeHtml(original)}}</div>` : ''}}
          <div class="detail-actions">
            <button class="primary" onclick="toggleBookmark('${{escapeHtml(item.item_id)}}', ${{bookmarked ? 'false' : 'true'}})">${{bookmarked ? '取消收藏' : '收藏'}}</button>
            <button onclick="openLogEditor('${{escapeHtml(item.item_id)}}')">写阅读日志</button>
            <a href="${{escapeHtml(item.source_url || '#')}}" target="_blank" rel="noreferrer">打开原文</a>
          </div>

          <div class="detail-section">
            <h3>摘要</h3>
            <div class="detail-text">${{escapeHtml(item.summary || '')}}</div>
          </div>

          <div class="detail-section">
            <h3>为什么重要</h3>
            <div class="detail-text">${{escapeHtml(item.importance_reason || '暂无说明')}}</div>
          </div>

          <div class="detail-section">
            <h3>来源信息</h3>
            <div class="detail-text">来源链接：<a href="${{escapeHtml(item.source_url || '#')}}" target="_blank" rel="noreferrer" style="color:#d4f7ff;text-decoration:underline;">打开原文</a></div>
          </div>

          <div class="detail-section">
            <h3>收藏备注</h3>
            <div class="detail-note-card">
              <textarea id="bookmarkNote" class="logbox" rows="4" placeholder="给这条收藏写个备注吧...">${{escapeHtml(bookmark?.note || '')}}</textarea>
              <div class="toolbar" style="margin-top:12px;">
                <button class="primary" onclick="saveBookmarkNote('${{escapeHtml(item.item_id)}}')">保存备注</button>
              </div>
            </div>
          </div>

          <div class="detail-section">
            <h3>历史日志</h3>
            <div class="small-list">
              ${{logs.length ? logs.map((log) => `
                <div class="small-item">
                  <div style="color:#fff;font-family:'Space Grotesk',sans-serif;font-size:16px;line-height:1.3;">${{escapeHtml(log.display_title || log.log_title || log.draft_title || log.title || '')}}</div>
                  <div style="color:var(--muted);font-size:12px;margin:8px 0 10px;">${{escapeHtml(log.created_at || '')}}</div>
                  <div class="detail-text">${{escapeHtml(log.log_text || '')}}</div>
                </div>
              `).join('') : '<div class="empty">暂无阅读日志，先生成初稿吧。</div>'}}
            </div>
          </div>
        </div>
      `;
    }}

    async function toggleBookmark(itemId, bookmarked) {{
      const item = findItemById(itemId);
      if (!item) return;
      const note = document.getElementById('bookmarkNote')?.value || '';
      const response = await fetch('/api/bookmarks', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ item_id: itemId, title: getDisplayTitle(item), report_id: REPORT_DATA.report_id, note, bookmarked }})
      }});
      const data = await response.json();
      if (data.ok) {{
        await loadLatest();
        openDetail(itemId);
      }} else {{
        alert(data.error || '收藏失败');
      }}
    }}

    async function saveBookmarkNote(itemId) {{
      const item = findItemById(itemId);
      if (!item) return;
      const note = document.getElementById('bookmarkNote')?.value || '';
      const response = await fetch('/api/bookmarks', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ item_id: itemId, title: getDisplayTitle(item), report_id: REPORT_DATA.report_id, note, bookmarked: true }})
      }});
      const data = await response.json();
      if (data.ok) {{
        await loadLatest();
        openDetail(itemId);
      }} else {{
        alert(data.error || '保存备注失败');
      }}
    }}

    async function openLogEditor(itemId) {{
      const item = findItemById(itemId);
      if (!item) return;
      document.getElementById('logItemId').value = itemId;
      document.getElementById('logTitle').value = getDisplayTitle(item) || '';
      document.getElementById('logText').value = '';
      document.getElementById('logDraftText').value = '';
      document.getElementById('logDraftMeta').textContent = '正在生成阅读日志初稿...';
      document.getElementById('logDialog').showModal();
      await generateLogDraft(true);
    }}

    async function generateLogDraft(silent = false) {{
      const itemId = document.getElementById('logItemId').value;
      if (!itemId) return;
      const response = await fetch('/api/log-draft', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{ item_id: itemId }})
      }});
      const data = await response.json();
      if (!data.ok) {{
        if (!silent) alert(data.error || '生成初稿失败');
        document.getElementById('logDraftMeta').textContent = data.error || '生成初稿失败';
        return;
      }}
      const draft = data.draft || {{}};
      document.getElementById('logText').value = draft.draft_text || '';
      document.getElementById('logDraftText').value = draft.draft_text || '';
      document.getElementById('logTitle').value = draft.draft_title || document.getElementById('logTitle').value || '';
      const metaParts = [];
      if (draft.generated_by) metaParts.push(`生成方式：${{draft.generated_by}}`);
      document.getElementById('logDraftMeta').textContent = metaParts.join(' · ');
      if (silent) {{
        const currentItem = findItemById(itemId);
        if (currentItem) openDetail(itemId);
      }}
    }}

    async function refreshDigest() {{
      const root = document.getElementById('cards');
      setRefreshLoading(true);
      startRefreshCountdown(180);
      root.innerHTML = '<div class="empty">正在重新抓取并生成日报，请稍候...</div>';
      const startedAt = Date.now();
      try {{
        const response = await fetch('/api/refresh', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }} }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || '刷新失败');
        }}
        REPORT_DATA.items = data.report.items || [];
        REPORT_DATA.bookmarks = data.report.bookmarks || [];
        REPORT_DATA.logs = data.report.logs || [];
        REPORT_DATA.themes = data.report.themes || [];
        REPORT_DATA.brief = data.report.brief || '';
        REPORT_DATA.title = data.report.title || '';
        REPORT_DATA.generated_at = data.report.generated_at || '';
        REPORT_DATA.report_id = data.report.report_id || REPORT_DATA.report_id;
        renderReport(data.report);
        openDetail((data.report.items || [])[0]?.item_id);
        const elapsed = data.elapsed_seconds || Math.max(1, Math.round((Date.now() - startedAt) / 1000));
        setRefreshLoading(false, `刷新完成，用时 ${{formatCountdown(elapsed)}}，已更新 ${{(data.report.items || []).length}} 条内容。`);
      }} catch (error) {{
        setRefreshLoading(false, `刷新失败：${{error.message || '未知错误'}}`);
        await loadLatest();
      }} finally {{
        if (refreshCountdownTimer) {{
          clearInterval(refreshCountdownTimer);
          refreshCountdownTimer = null;
        }}
      }}
    }}

    function attachHandlers() {{
      document.querySelectorAll('[data-item-id]').forEach((card) => {{
        const itemId = card.dataset.itemId;
        card.onclick = (event) => {{
          if (event.target.closest('button') || event.target.closest('a')) return;
          openDetail(itemId);
        }};
      }});
      document.querySelectorAll('[data-action="bookmark"]').forEach((btn) => {{
        btn.onclick = async () => {{
          const item = btn.closest('[data-item-id]');
          const itemId = item.dataset.itemId;
          const selected = findItemById(itemId);
          const response = await fetch('/api/bookmarks', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{ item_id: itemId, title: getDisplayTitle(selected), report_id: REPORT_DATA.report_id, bookmarked: true }})
          }});
          const data = await response.json();
          if (data.ok) {{
            await loadLatest();
            openDetail(itemId);
          }} else {{
            alert(data.error || '收藏失败');
          }}
        }};
      }});
      document.querySelectorAll('[data-action="log"]').forEach((btn) => {{
        btn.onclick = () => {{
          const item = btn.closest('[data-item-id]');
          openLogEditor(item.dataset.itemId);
        }};
      }});
      document.querySelectorAll('.bookmark-card').forEach((card) => {{
        card.onclick = () => openDetail(card.dataset.bookmarkId);
      }});
      document.querySelectorAll('.log-card').forEach((card) => {{
        card.onclick = () => openDetail(card.dataset.logItemId);
      }});
    }}

    async function submitLog(event) {{
      event.preventDefault();
      const itemId = document.getElementById('logItemId').value;
      const logText = document.getElementById('logText').value.trim();
      if (!logText) return;
      const response = await fetch('/api/logs', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          item_id: itemId,
          log_text: logText,
          draft_text: document.getElementById('logDraftText').value || '',
          title: document.getElementById('logTitle').value || '',
          log_title: document.getElementById('logTitle').value || ''
        }})
      }});
      const data = await response.json();
      if (data.ok) {{
        document.getElementById('logText').value = '';
        document.getElementById('logDialog').close();
        await loadLatest();
        openDetail(itemId);
        alert('日志已保存');
      }} else {{
        alert(data.error || '保存失败');
      }}
    }}

    if (REPORT_DATA.items) {{
      renderReport(REPORT_DATA);
      if (REPORT_DATA.items.length) {{
        openDetail(REPORT_DATA.items[0].item_id);
      }}
    }}

    attachHandlers();
  </script>
</body>
</html>"""


def send_feishu_digest(webhook_url: str, report: Dict[str, Any], dashboard_url: str = "") -> bool:
    """发送带按钮的飞书卡片摘要。按钮主要跳转到网页端。"""
    if not webhook_url:
        return False

    items = report.get("items", [])[:8]
    elements = []
    for idx, item in enumerate(items, start=1):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{idx}. {item.get('title', '')}**\n{item.get('summary', '')[:180]}",
                },
            }
        )
        elements.append({"tag": "hr"})

    buttons = []
    for item in items[:3]:
        target = f"{dashboard_url}#item={item.get('item_id','')}" if dashboard_url else item.get("source_url", "")
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"打开 {item.get('title','')[:8]}"},
                "url": target or dashboard_url,
                "type": "default",
            }
        )

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": report.get("title", f"{report.get('app_name', 'DAY VIBE AI')} 24 小时速览")},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"{report.get('brief', '')}\n\n生成时间：{report.get('generated_at', '')}\n选中：{len(items)} 条",
                    },
                },
                {"tag": "hr"},
                *elements,
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开日报网页"},
                            "url": dashboard_url or report.get("items", [{}])[0].get("source_url", ""),
                            "type": "primary",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "查看原文"},
                            "url": report.get("items", [{}])[0].get("source_url", "") if report.get("items") else dashboard_url,
                            "type": "default",
                        },
                    ],
                },
            ],
        },
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        return 200 <= response.status_code < 300
    except Exception:
        return False
