"""广东省疾病预防控制中心 - 列表页解析模块。

适用栏目：
- 要闻动态 / 监测信息
- 政务公开 / 通知公告、政策法规、技术标准与文件、政策解读、数据发布、疫情信息
- 健康教育 / 各健康主题子栏目

说明：
1. 输出仍使用项目统一 schema。
2. 一级栏目写入 source.channel_name。
3. 二级/三级栏目不新增顶层字段，只写入 classification.topic_tags 和 crawl.asset_subdir_parts。
4. crawl.asset_subdir_parts 会被 main.py 用来保存资源路径：
   机构 / 一级栏目 / 二级栏目 / 文章 / 附件或图片
"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


FILE_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".csv", ".txt", ".zip", ".rar", ".7z",
    ".mp4", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".webm", ".m3u8"
)

INVALID_TITLE_KEYWORDS = (
    "首页", "上一页", "下一页", "尾页", "末页", "最后一页",
    "返回", "更多", "TOP", "打印"
)


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """
    构造广东省疾控中心分页 URL。

    常见规则：
        第 1 页：index.html 或 栏目目录 /
        第 2 页：index_2.html
        第 3 页：index_3.html

    如果配置误填了 index_3.html，本函数会自动从同目录 index.html 开始构造，
    避免漏抓第 1、2 页。
    """
    if max_pages <= 1:
        return [first_url]

    url = first_url.strip()
    page_urls: list[str] = []

    match = re.search(r"/index_(\d+)\.html(?:$|\?)", url)
    if match:
        url = re.sub(r"index_\d+\.html(?:$|\?)", "index.html", url)

    if url.endswith("/"):
        page_urls.append(url)
        for page_no in range(2, max_pages + 1):
            page_urls.append(urljoin(url, f"index_{page_no}.html"))
        return page_urls

    if url.endswith("index.html"):
        prefix = url[: -len("index.html")]
        page_urls.append(url)
        for page_no in range(2, max_pages + 1):
            page_urls.append(f"{prefix}index_{page_no}.html")
        return page_urls

    return [url]


def _safe_text(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    text = clean_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_file_url(url: str) -> bool:
    clean_url = (url or "").lower().split("?")[0].split("#")[0]
    return clean_url.endswith(FILE_SUFFIXES)


def _infer_file_type(url: str, title: str = "") -> str:
    candidates = [url or "", title or ""]
    for text in candidates:
        match = re.search(
            r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z|mp4|mov|m4v|avi|wmv|flv|webm|m3u8)(?:$|\?|#)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).lower()
    return "bin"


def _same_domain_or_relative(url: str, site_domain: str) -> bool:
    parsed = urlparse(url)

    if not parsed.netloc:
        return True

    return site_domain in parsed.netloc


def _is_valid_article_or_file_url(url: str, site_domain: str) -> bool:
    if not url:
        return False

    lower_url = url.lower().strip()

    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    if not _same_domain_or_relative(url, site_domain):
        return False

    parsed_path = urlparse(url).path

    if "/content/post_" in parsed_path:
        return True

    if _is_file_url(url):
        return True

    return False


def _extract_id_from_url(url: str, fallback: int) -> int:
    patterns = [
        r"post_(\d+)",
        r"/attachment/\d+/\d+/(\d+)\.",
        r"/P0?(\d+)\.",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                continue

    return fallback


def _apply_channel_hierarchy(item: dict, channel: dict) -> None:
    """
    将 sites.json 里的层级信息写到已有 schema 字段中。

    不新增顶层字段：
    - classification.topic_tags：用于检索/统计
    - crawl.asset_subdir_parts：用于 main.py 保存附件和图片的多级目录
    - raw：保存少量原始层级，方便人工检查
    """
    sub_channel_name = channel.get("sub_channel_name", "") or ""
    asset_parts = channel.get("asset_subdir_parts", [])

    if isinstance(asset_parts, str):
        asset_parts = [asset_parts]

    if not asset_parts and sub_channel_name:
        asset_parts = [sub_channel_name]

    asset_parts = [str(part).strip() for part in asset_parts if str(part).strip()]

    if sub_channel_name:
        item["raw"]["sub_channel_name"] = sub_channel_name

    if asset_parts:
        item["crawl"]["asset_subdir_parts"] = asset_parts

    for part in asset_parts:
        if part not in item["classification"]["topic_tags"]:
            item["classification"]["topic_tags"].append(part)


def _build_item(
    *,
    title: str,
    detail_url: str,
    publish_date: str,
    channel: dict,
    site_config: dict,
    index: int,
) -> dict:
    site_domain = site_config.get("site_domain", "")

    item = build_empty_document(site_config, channel)

    url_index = _extract_id_from_url(detail_url, index)

    item["doc_id"] = generate_doc_id(
        site_domain,
        channel.get("channel_name", ""),
        url_index,
        publish_date,
    )

    item["title"] = title
    item["url"] = detail_url
    item["dates"]["publish_date"] = publish_date

    item["raw"]["raw_title"] = title
    item["raw"]["raw_date"] = publish_date
    item["raw"]["raw_source"] = ""

    _apply_channel_hierarchy(item, channel)

    if _is_file_url(detail_url):
        file_type = _infer_file_type(detail_url, title)
        file_name = title

        if file_type != "bin" and not file_name.lower().endswith(f".{file_type}"):
            file_name = f"{file_name}.{file_type}"

        item["attachments"].append({
            "name": file_name,
            "url": detail_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        item["crawl"]["crawl_status"] = "direct_file"
    else:
        item["crawl"]["crawl_status"] = "list_parsed"

    return item


def _select_list_nodes(soup: BeautifulSoup) -> list:
    selectors = [
        "div.right.newsList ul.list > li",
        "div.right ul.list > li",
        "ul.list > li",
        "div.newsList li",
    ]

    for selector in selectors:
        nodes = soup.select(selector)
        if nodes:
            return nodes

    return []


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析列表页，返回统一 schema item 列表。"""
    soup = BeautifulSoup(html, "lxml")

    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))

    records: list[dict] = []
    seen_urls: set[str] = set()

    nodes = _select_list_nodes(soup)

    for node in nodes:
        a_tag = node.find("a", href=True)
        if not a_tag:
            continue

        href = a_tag.get("href", "").strip()
        detail_url = urljoin(base_url, href)

        if detail_url in seen_urls:
            continue

        if not _is_valid_article_or_file_url(detail_url, site_domain):
            continue

        title = (
            _safe_text(a_tag.get("title", ""))
            or _safe_text(a_tag.get_text(" ", strip=True))
        )

        if not title or len(title) < 4:
            continue

        if title in INVALID_TITLE_KEYWORDS:
            continue

        if any(key in title for key in INVALID_TITLE_KEYWORDS):
            continue

        node_text = _safe_text(node.get_text(" ", strip=True))
        publish_date = ""

        time_node = node.select_one("span.time, .time")
        if time_node:
            publish_date = extract_date(time_node.get_text(" ", strip=True))

        if not publish_date:
            publish_date = extract_date(node_text)

        item = _build_item(
            title=title,
            detail_url=detail_url,
            publish_date=publish_date,
            channel=channel,
            site_config=site_config,
            index=len(records) + 1,
        )

        records.append(item)
        seen_urls.add(detail_url)

    return records
