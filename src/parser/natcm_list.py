"""国家中医药管理局官网 - 栏目列表页解析模块。

适用栏目：
    - 政策文件：http://www.natcm.gov.cn/a/zcwj/
    - 政策解读：http://www.natcm.gov.cn/a/zcjd/
    - 通知公告：http://www.natcm.gov.cn/a/tzgg/

分页示例：
    http://www.natcm.gov.cn/a/zcwj/index_2.html
    http://www.natcm.gov.cn/a/zcjd/index_2.html
    http://www.natcm.gov.cn/a/tzgg/index_2.html

保存路径：
    src/parser/natcm_list.py
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


DETAIL_SUFFIX_RE = re.compile(r"\.html(?:$|\?)", re.IGNORECASE)
INVALID_TITLE_KEYWORDS = {
    "首页",
    "上一页",
    "下一页",
    "末页",
    "尾页",
    "更多",
    "返回",
    "政策文件",
    "政策解读",
    "通知公告",
    "国家中医药管理局",
}
CHANNEL_INDEX_RE = re.compile(r"^/a/(?:zcwj|zcjd|tzgg)/(?:index(?:_\d+)?\.html)?$", re.IGNORECASE)


def _is_valid_detail_url(url: str, site_domain: str) -> bool:
    """过滤导航、分页、空链接、非本站链接。"""
    if not url:
        return False

    lower_url = url.lower().strip()
    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    parsed = urlparse(url)
    if parsed.netloc and site_domain and site_domain not in parsed.netloc:
        return False

    if not DETAIL_SUFFIX_RE.search(parsed.path):
        return False

    # 过滤栏目分页页，例如 /a/zcwj/index_2.html。
    if CHANNEL_INDEX_RE.match(parsed.path):
        return False

    # 该站详情页通常含日期目录：/xxx/.../2024-01-26/33151.html。
    # 不强制日期目录，避免漏掉个别历史稿件，只过滤明显的 index 页。
    if parsed.path.rsplit("/", 1)[-1].lower().startswith("index"):
        return False

    return True


def _extract_title(a_tag) -> str:
    """从 a 标签提取标题，兼容 title 属性和正文文本。"""
    title = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
    title = re.sub(r"\s+", " ", title).strip()
    if not title or title in INVALID_TITLE_KEYWORDS:
        return ""
    return title


def _extract_publish_date(node) -> str:
    """优先从 span/time 提取日期，再从整条 li 文本兜底。"""
    date_node = node.select_one("time, span") if getattr(node, "select_one", None) else None
    if date_node:
        date_text = clean_text(date_node.get("datetime", "")) or clean_text(date_node.get_text(" ", strip=True))
        publish_date = extract_date(date_text)
        if publish_date:
            return publish_date

    text = clean_text(node.get_text(" ", strip=True)) if getattr(node, "get_text", None) else ""
    return extract_date(text)


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析国家中医药管理局栏目列表页，返回统一 schema 的初步数据。"""
    soup = BeautifulSoup(html, "lxml")
    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))

    records: list[dict] = []
    seen_urls: set[str] = set()

    # 该站老模板常见结构：ul.lbbt > li > a + span(date)。
    candidate_nodes = soup.select("ul.lbbt li")

    # 兜底：页面结构轻微变化时，从 box 中取 li。
    if not candidate_nodes:
        candidate_nodes = soup.select("table.box li, .box li, li")

    # 再兜底：直接扫描所有 a。
    if not candidate_nodes:
        candidate_nodes = soup.select("a[href]")

    for node in candidate_nodes:
        a_tag = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
        if not a_tag:
            continue

        title = _extract_title(a_tag)
        if not title or len(title) < 4:
            continue

        href = (a_tag.get("href") or "").strip()
        detail_url = urljoin(base_url, href)
        if not _is_valid_detail_url(detail_url, site_domain):
            continue
        if detail_url in seen_urls:
            continue

        publish_date = _extract_publish_date(node)

        item = build_empty_document(site_config, channel)
        item["doc_id"] = generate_doc_id(
            site_domain,
            channel.get("channel_name", ""),
            len(records) + 1,
            publish_date,
        )
        item["title"] = title
        item["url"] = detail_url
        item["dates"]["publish_date"] = publish_date
        item["crawl"]["crawl_status"] = "list_parsed"
        item["raw"]["raw_title"] = title
        item["raw"]["raw_date"] = publish_date

        records.append(item)
        seen_urls.add(detail_url)

    return records


def _normalize_channel_base(first_url: str) -> str:
    """把栏目 URL 统一为目录形式：.../a/zcwj/。"""
    parsed = urlparse(first_url)
    path = parsed.path or "/"

    # .../index_2.html 或 .../index.html -> 目录。
    path = re.sub(r"index(?:_\d+)?\.html$", "", path, flags=re.IGNORECASE)

    # 若传入的是目录但没斜杠，补斜杠。
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """构造分页 URL。

    该站分页规则：
        第 1 页：http://www.natcm.gov.cn/a/zcwj/
        第 2 页：http://www.natcm.gov.cn/a/zcwj/index_2.html
        第 n 页：http://www.natcm.gov.cn/a/zcwj/index_n.html
    """
    if max_pages <= 1:
        return [first_url]

    base = _normalize_channel_base(first_url)
    urls: list[str] = []
    for page_no in range(1, max_pages + 1):
        if page_no == 1:
            urls.append(base)
        else:
            urls.append(urljoin(base, f"index_{page_no}.html"))
    return urls
