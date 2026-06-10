"""爬虫通用工具函数。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_HTML_DIR = DATA_DIR / "raw_html"
OUTPUT_DIR = DATA_DIR / "output"
LOG_DIR = DATA_DIR / "logs"
ATTACHMENT_DIR = DATA_DIR / "attachments"
IMAGE_DIR = DATA_DIR / "images"


def ensure_dirs() -> None:
    """创建项目需要的数据目录。"""
    for path in (RAW_HTML_DIR, OUTPUT_DIR, LOG_DIR, ATTACHMENT_DIR, IMAGE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_json(path: str | Path) -> dict:
    """读取 JSON 配置文件。"""
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def save_jsonl(items: list[dict], output_path: str | Path) -> None:
    """批量保存 JSONL 文件。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def append_jsonl(item: dict, output_path: str | Path) -> None:
    """追加保存单条 JSONL 数据。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def save_raw_html(html: str, doc_id: str) -> str:
    """保存原始 HTML，并返回相对项目根目录的路径。"""
    safe_doc_id = re.sub(r"[^\w.-]+", "_", doc_id, flags=re.UNICODE)
    file_path = RAW_HTML_DIR / f"{safe_doc_id}.html"
    file_path.write_text(html, encoding="utf-8")
    # return str(file_path.relative_to(PROJECT_ROOT))
    return file_path.relative_to(PROJECT_ROOT).as_posix()


def generate_doc_id(site_domain: str, channel_name: str, index: int, publish_date: str = "") -> str:
    """生成稳定的文档 ID。

    ID 中包含站点、栏目、日期和序号，便于人工排查。
    """
    domain_part = re.sub(r"\W+", "_", site_domain).strip("_")
    channel_part = re.sub(r"\W+", "_", channel_name).strip("_")
    date_part = publish_date.replace("-", "") if publish_date else datetime.now().strftime("%Y%m%d")
    return f"{domain_part}_{channel_part}_{date_part}_{index:04d}"


def clean_text(text: str) -> str:
    """清洗文本中的多余空白。"""
    if not text:
        return ""
    text = text.replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def extract_date(text: str) -> str:
    """从文本中提取 YYYY-MM-DD 日期。"""
    if not text:
        return ""
    match = re.search(r"(20\d{2}|19\d{2})[-./年](\d{1,2})[-./月](\d{1,2})日?", text)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def text_hash(text: str) -> str:
    """生成正文 MD5 哈希，用于后续去重。"""
    if not text:
        return ""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def infer_document_type(title: str) -> str:
    """根据标题关键词推断文件类型。"""
    title = title or ""
    rules = [
        ("通知", "通知"),
        ("公告", "公告"),
        ("通告", "通告"),
        ("意见", "意见"),
        ("办法", "办法"),
        ("方案", "方案"),
        ("指南", "指南"),
        ("规范", "规范"),
        ("标准", "标准"),
        ("公报", "统计公报"),
        ("解读", "政策解读")
    ]
    for keyword, document_type in rules:
        if keyword in title:
            return document_type
    return "其他"


def infer_policy_category(title: str, channel_name: str, default_category: str) -> str:
    """根据标题和栏目名推断政策类别。"""
    text = f"{title or ''} {channel_name or ''}"
    category_rules = [
        (("传染病", "疾控", "疫情", "突发公共卫生"), "疾病防控"),
        (("食品安全", "三新食品", "食品"), "食品安全"),
        (("职业病", "职业健康"), "职业健康"),
        (("妇幼", "儿童", "孕产妇"), "妇幼健康"),
        (("老年", "老龄", "护理"), "老龄健康"),
        (("医疗质量", "医院", "诊疗", "医疗服务"), "医疗服务"),
        (("统计", "公报", "数据"), "统计数据")
    ]
    for keywords, category in category_rules:
        if any(keyword in text for keyword in keywords):
            return category
    if "工作通知" in (channel_name or ""):
        return "工作通知"
    return default_category


def setup_logger(log_path: str | Path) -> logging.Logger:
    """初始化日志，同时输出到文件和控制台。"""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("nhc_crawler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def build_empty_document(site_config: dict[str, Any], channel: dict[str, Any]) -> dict:
    """创建统一 JSON schema 的空文档。"""
    now_date = datetime.now().strftime("%Y-%m-%d")
    return {
        "doc_id": "",
        "title": "",
        "url": "",
        "source": {
            "site_name": site_config.get("site_name", ""),
            "site_domain": site_config.get("site_domain", ""),
            "site_url": site_config.get("site_url", ""),
            "channel_name": channel.get("channel_name", ""),
            "channel_url": channel.get("channel_url", "")
        },
        "organization": {
            "source_department": "",
            "issuing_authority": [],
            "joint_departments": []
        },
        "classification": {
            "policy_level": site_config.get("policy_level", ""),
            "document_type": "",
            "policy_category": channel.get("default_policy_category", ""),
            "topic_tags": [],
            "target_region": "全国"
        },
        "dates": {
            "publish_date": "",
            "crawl_date": now_date
        },
        "content": {
            "body_text": "",
            "body_html": "",
        },
        "attachments": [],
        "images": [],
        "crawl": {
            "crawler_name": site_config.get("crawler_name", ""),
            "crawl_status": "",
            "http_status": None,
            "raw_html_path": "",
            "text_hash": "",
            "error_message": ""
        },
        "raw": {
            "raw_title": "",
            "raw_date": "",
            "raw_source": ""
        }
    }
