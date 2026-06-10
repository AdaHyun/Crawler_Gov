"""列表页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


DETAIL_SUFFIX_RE = re.compile(r"\.(shtml|html)(?:$|\?)", re.IGNORECASE)
INVALID_TITLE_KEYWORDS = ("首页", "上一页", "下一页", "末页", "尾页", "更多", "返回")


def _is_valid_detail_url(url: str, site_domain: str) -> bool:
    """过滤导航链接、空链接和非详情页链接。"""
    if not url:
        return False
    lower_url = url.lower()
    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    parsed = urlparse(url)
    if parsed.netloc and site_domain not in parsed.netloc:
        return False
    if not DETAIL_SUFFIX_RE.search(parsed.path):
        return False
    if parsed.path.endswith("/list.shtml"):
        return False
    return True


def _extract_title(a_tag) -> str:
    """从 a 标签提取标题，兼容 title 属性和正文文本。"""
    title = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
    for keyword in INVALID_TITLE_KEYWORDS:
        if title == keyword:
            return ""
    return title


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析栏目列表页，返回统一 schema 的初步数据。

    当前优先从 li 标签解析，若页面结构变化，也会兜底扫描页面中的所有 a 标签。
    """
    soup = BeautifulSoup(html, "lxml")
    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))
    records: list[dict] = []
    seen_urls: set[str] = set()

    # 优先解析 li，政府网站列表页通常是一条 li 对应一个文件。
    candidate_nodes = soup.select("li")
    if not candidate_nodes:
        candidate_nodes = soup.select("a")

    for node in candidate_nodes:
        a_tag = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
        if not a_tag:
            continue

        title = _extract_title(a_tag)
        href = a_tag.get("href", "").strip()
        detail_url = urljoin(base_url, href)

        if not title or len(title) < 4:
            continue
        if not _is_valid_detail_url(detail_url, site_domain):
            continue
        if detail_url in seen_urls:
            continue

        node_text = clean_text(node.get_text(" ", strip=True))
        publish_date = extract_date(node_text)

        item = build_empty_document(site_config, channel)
        item["doc_id"] = generate_doc_id(site_domain, channel.get("channel_name", ""), len(records) + 1, publish_date)
        item["title"] = title
        item["url"] = detail_url
        item["dates"]["publish_date"] = publish_date
        item["crawl"]["crawl_status"] = "list_parsed"
        item["raw"]["raw_title"] = title
        item["raw"]["raw_date"] = publish_date

        records.append(item)
        seen_urls.add(detail_url)

    return records

def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """卫健委的专有翻页规则"""
    if max_pages <= 1:
        return [first_url]
    page_urls = [first_url]
    if first_url.endswith("list.shtml"):
        prefix = first_url[:-len("list.shtml")]
        for page_no in range(2, max_pages + 1):
            page_urls.append(f"{prefix}list_{page_no}.shtml")
    return page_urls
