"""中国老龄协会官网（银龄在线）- 健康科普栏目列表页解析模块。

适用栏目：
    - 健康科普

示例列表页：
    https://www.cncaprc.gov.cn/jkkp.jhtml
    https://www.cncaprc.gov.cn/jkkp_2.jhtml

保存路径：
    src/parser/cncaprc_list.py
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


DETAIL_SUFFIX_RE = re.compile(r"\.jhtml(?:$|\?)", re.IGNORECASE)
INVALID_TITLE_KEYWORDS = {
    "首页",
    "上一页",
    "下一页",
    "末页",
    "尾页",
    "更多",
    "返回",
    "健康科普",
    "银龄在线",
}


def _is_valid_detail_url(url: str, site_domain: str) -> bool:
    """过滤导航链接、空链接、非本站链接和栏目页。"""
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

    # 过滤栏目页和分页页，例如 /jkkp.jhtml、/jkkp_2.jhtml。
    path_name = parsed.path.rsplit("/", 1)[-1]
    if re.fullmatch(r"jkkp(?:_\d+)?\.jhtml", path_name, flags=re.IGNORECASE):
        return False

    return True


def _extract_title(a_tag) -> str:
    """从 a 标签提取标题，兼容 title 属性和正文文本。"""
    title = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
    title = re.sub(r"\s+", " ", title).strip()
    if not title or title in INVALID_TITLE_KEYWORDS:
        return ""
    return title


def _extract_summary(node) -> str:
    """提取列表页摘要。"""
    desc_node = node.select_one(".channel-news-desc, .news-desc, p")
    if desc_node:
        return clean_text(desc_node.get_text(" ", strip=True))
    return ""


def _extract_publish_date(node) -> str:
    """优先从 time 标签提取日期，再从整条文本兜底。"""
    time_node = node.select_one("time, .channel-news-date, .news-date, .date")
    if time_node:
        date_text = clean_text(time_node.get("datetime", "")) or clean_text(time_node.get_text(" ", strip=True))
        publish_date = extract_date(date_text)
        if publish_date:
            return publish_date
    return extract_date(clean_text(node.get_text(" ", strip=True)))


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析健康科普列表页，返回统一 schema 的初步数据。"""
    soup = BeautifulSoup(html, "lxml")
    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))

    records: list[dict] = []
    seen_urls: set[str] = set()

    # 该站健康科普列表结构：li.channel-news-item > div.channel-news-info > h3 > a。
    candidate_nodes = soup.select("li.channel-news-item")

    # 兜底：如果结构微调，尝试从新闻列表区域里找 li。
    if not candidate_nodes:
        candidate_nodes = soup.select(".channel-news-list li, section.channel-content li, ul li")

    # 再兜底：直接扫描所有 a，防止页面结构变化。
    if not candidate_nodes:
        candidate_nodes = soup.select("a[href]")

    for node in candidate_nodes:
        a_tag = node if getattr(node, "name", "") == "a" else node.select_one("h3 a[href], .channel-news-title a[href], a[href]")
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
        summary = _extract_summary(node)

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

        # 不破坏统一 schema：如果 build_empty_document 里有 summary 字段就写入，没有也不强依赖。
        if summary:
            item.setdefault("content", {})["summary"] = summary
            item.setdefault("raw", {})["raw_summary"] = summary

        records.append(item)
        seen_urls.add(detail_url)

    return records


def _strip_page_suffix(url: str) -> tuple[str, str, str]:
    """把 /jkkp_2.jhtml 或 /jkkp.jhtml 拆成：前缀、扩展名、query。"""
    parsed = urlparse(url)
    path = parsed.path
    query = parsed.query

    match = re.match(r"^(?P<prefix>.*?)(?:_\d+)?(?P<suffix>\.jhtml)$", path, re.IGNORECASE)
    if not match:
        return url, "", query

    prefix = match.group("prefix")
    suffix = match.group("suffix")
    base = urlunparse((parsed.scheme, parsed.netloc, prefix, "", "", ""))
    return base, suffix, query


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """构造分页 URL。

    该站分页规则：
        第 1 页：/jkkp.jhtml
        第 2 页：/jkkp_2.jhtml
        第 n 页：/jkkp_n.jhtml

    即使传入的是 /jkkp_2.jhtml，也会从 /jkkp.jhtml 开始构造，避免漏掉首页。
    """
    if max_pages <= 1:
        return [first_url]

    base, suffix, query = _strip_page_suffix(first_url)
    if not suffix:
        return [first_url]

    urls: list[str] = []
    for page_no in range(1, max_pages + 1):
        if page_no == 1:
            url = f"{base}{suffix}"
        else:
            url = f"{base}_{page_no}{suffix}"
        if query:
            url = f"{url}?{query}"
        urls.append(url)
    return urls
