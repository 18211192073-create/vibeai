# coding=utf-8
"""
DAY VIBE AI

提供日报生成、本地存储、网页服务和飞书辅助推送能力。
"""

from .digest import build_daily_digest, send_feishu_digest, load_assistant_settings
from .storage import AssistantStorage

__all__ = [
    "AssistantStorage",
    "build_daily_digest",
    "send_feishu_digest",
    "load_assistant_settings",
]
