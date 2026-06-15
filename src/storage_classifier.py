"""Storage-path classification helpers for downloaded crawler files."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = PROJECT_ROOT / "config" / "reclassify_rules.json"

INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')
STANDARD_CODE_RE = re.compile(r"^\s*(GB\s*[_/]?\s*T|WS\s*[_/]?\s*T|WST|GB|WS)(?=[\s._/-]*\d|\b)", re.IGNORECASE)


def safe_path_part(value: str, fallback: str = "未命名") -> str:
    """Return a Windows-safe path component."""
    value = INVALID_PATH_CHARS.sub("_", value or "").strip()
    return value or fallback


@lru_cache(maxsize=1)
def load_storage_rules() -> dict[str, Any]:
    if not RULES_PATH.exists():
        return {"targets": []}
    with RULES_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
        return {"targets": []}
    return data


def normalize_standard_code(title: str) -> str:
    match = STANDARD_CODE_RE.search(title or "")
    if not match:
        return "未识别标准号"
    raw = re.sub(r"[\s/]+", "_", match.group(1).upper()).replace("__", "_")
    if raw in {"WST", "WS_T"}:
        return "WS_T"
    if raw == "GB_T":
        return "GB_T"
    return raw


def keyword_group(title: str, groups: list[dict[str, Any]], default: str) -> str:
    for group in groups:
        name = str(group.get("name", "")).strip()
        keywords = [str(keyword) for keyword in group.get("keywords", [])]
        if name and any(keyword and keyword in title for keyword in keywords):
            return name
    return default


def classify_storage(site_name: str, channel_name: str, title: str) -> list[str]:
    """Return storage categories, always starting with the source channel name."""
    rules = load_storage_rules()
    for rule in rules.get("targets", []):
        if rule.get("site") != site_name or rule.get("channel") != channel_name:
            continue

        classifier = rule.get("classifier", "keyword_groups")
        groups = rule.get("groups", [])
        default = str(rule.get("default", "未分类"))
        topic = keyword_group(title or "", groups, default)

        levels = [channel_name, topic]
        if classifier == "standard_topic" and rule.get("code_level", False):
            levels.append(normalize_standard_code(title or ""))
        return [safe_path_part(level, "未分类") for level in levels]

    return [safe_path_part(channel_name, "未分类")]
