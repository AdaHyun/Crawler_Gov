"""食品安全国家标准数据检索平台 - 列表页解析模块。

适用栏目：
    - 标准文本
    - 标准勘误
    - 标准解读

保存路径：
    src/parser/cfsa_list.py
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)

INVALID_TITLE_KEYWORDS = {
    "首页",
    "上一页",
    "下一页",
    "末页",
    "尾页",
    "更多",
    "返回",
    "预览",
    "下载",
    "标准文本",
    "标准勘误",
    "标准解读",
}

DEFAULT_TYPE_BY_CHANNEL = {
    "标准文本": "2",
    "标准勘误": "3",
    "标准解读": "4",
}


def _get_channel_type(channel: dict) -> str:
    """从栏目配置中读取 type；没有配置时按栏目名兜底。"""
    value = channel.get("query_type") or channel.get("type") or ""
    if value:
        return str(value)

    channel_name = channel.get("channel_name", "")
    for key, default_type in DEFAULT_TYPE_BY_CHANNEL.items():
        if key in channel_name:
            return default_type
    return "2"


def _db_base_url(base_url: str) -> str:
    """把任意栏目 URL 规整到 /db 入口，用来拼详情页。"""
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        return base_url
    return urlunparse((parsed.scheme, parsed.netloc, "/db", "", "", ""))


def _normalize_detail_url(base_url: str, href: str, channel_type: str, node_html: str = "") -> str:
    """从 href、onclick 或节点 HTML 中恢复详情页 URL。"""
    href = (href or "").strip()

    # 已经是可访问的详情 URL。
    if href and not href.lower().startswith(("javascript:", "#", "mailto:")):
        full_url = urljoin(base_url, href)
        if "guid=" in full_url.lower() or "/db" in urlparse(full_url).path.lower():
            return full_url

    # href / onclick / 整个节点中只要出现 UUID，就拼成 /db?type=x&guid=UUID。
    combined = f"{href} {node_html}"
    guid_match = UUID_RE.search(combined)
    if guid_match:
        db_url = _db_base_url(base_url)
        return f"{db_url}?type={channel_type}&guid={guid_match.group(0).upper()}"

    return ""


def _is_valid_detail_url(url: str, site_domain: str) -> bool:
    """过滤导航、空链接和非本站链接。"""
    if not url:
        return False

    parsed = urlparse(url)
    if parsed.netloc and site_domain and site_domain not in parsed.netloc:
        return False

    query = parse_qs(parsed.query)
    if query.get("guid"):
        return True

    # 保留兜底：如果以后该站改为 html 详情页，也能解析。
    lower_path = parsed.path.lower()
    return lower_path.endswith((".html", ".shtml")) and not lower_path.endswith("/list.shtml")


def _extract_title_from_node(node) -> str:
    """优先从 .infoItemCenter 提取标题，避开“预览/下载”按钮。"""
    candidates = []

    center = node.select_one(".infoItemCenter")
    if center:
        candidates.extend(center.select("a"))
        candidates.append(center)

    candidates.extend(node.select("a"))

    for cand in candidates:
        text = clean_text(cand.get("title", "")) or clean_text(cand.get_text(" ", strip=True))
        if not text:
            continue

        # 去掉日期行和按钮文字。
        text = re.sub(r"发布日期\s*[:：].*$", "", text).strip()
        text = re.sub(r"实施日期\s*[:：].*$", "", text).strip()
        text = clean_text(text)

        if text and text not in INVALID_TITLE_KEYWORDS and len(text) >= 4:
            return text

    # 兜底：直接从整条 item 文本里切出标题。
    node_text = clean_text(node.get_text(" ", strip=True))
    node_text = re.sub(r"^(标准文本|标准勘误|标准解读)\s*", "", node_text)
    node_text = re.sub(r"(预览|下载)\s*$", "", node_text)
    node_text = re.split(r"发布日期\s*[:：]", node_text)[0]
    node_text = clean_text(node_text)
    if node_text and node_text not in INVALID_TITLE_KEYWORDS:
        return node_text
    return ""


def _extract_dates(node_text: str) -> tuple[str, str]:
    """提取发布日期和实施日期。"""
    publish_date = ""
    implementation_date = ""

    pub_match = re.search(
        r"发布日期\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
        node_text,
    )
    if pub_match:
        publish_date = extract_date(pub_match.group(1))

    impl_match = re.search(
        r"实施日期\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
        node_text,
    )
    if impl_match:
        implementation_date = extract_date(impl_match.group(1))

    if not publish_date:
        publish_date = extract_date(node_text)

    return publish_date, implementation_date


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析列表页，返回统一 schema 的初步记录。"""
    soup = BeautifulSoup(html, "lxml")
    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))
    channel_type = _get_channel_type(channel)

    records: list[dict] = []
    seen_urls: set[str] = set()

    # 该平台列表项结构：div#mainDiv.infoList > div.infoItem
    candidate_nodes = soup.select("#mainDiv .infoItem, .infoList .infoItem, div.infoItem")

    # 兜底：如果页面结构调整，尝试从所有包含 GUID 的节点解析。
    if not candidate_nodes:
        candidate_nodes = []
        for node in soup.select("div, li"):
            if UUID_RE.search(str(node)):
                candidate_nodes.append(node)

    for node in candidate_nodes:
        node_html = str(node)
        title = _extract_title_from_node(node)
        if not title:
            continue

        detail_url = ""

        # 优先取标题链接、预览按钮等 a 标签中的 href/onclick。
        for a_tag in node.select("a"):
            href = a_tag.get("href", "")
            onclick = a_tag.get("onclick", "")
            detail_url = _normalize_detail_url(base_url, href, channel_type, f"{onclick} {node_html}")
            if detail_url:
                break

        # 兜底从整个节点里找 GUID。
        if not detail_url:
            detail_url = _normalize_detail_url(base_url, "", channel_type, node_html)

        if not _is_valid_detail_url(detail_url, site_domain):
            continue
        if detail_url in seen_urls:
            continue

        node_text = clean_text(node.get_text(" ", strip=True))
        publish_date, implementation_date = _extract_dates(node_text)

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
        if implementation_date:
            item["dates"]["implementation_date"] = implementation_date

        item["crawl"]["crawl_status"] = "list_parsed"
        item["raw"]["raw_title"] = title
        item["raw"]["raw_date"] = publish_date
        item["raw"]["raw_list_text"] = node_text

        records.append(item)
        seen_urls.add(detail_url)

    return records


def _replace_or_add_query(url: str, **params: str) -> str:
    """替换或新增 URL query 参数。"""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key, value in params.items():
        query[key] = [str(value)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """构造列表页 URL。

    说明：
    1. 如果 sites.json 的 channel_url 写成 "...page={page}"，会按模板生成。
    2. 如果 URL 里已有 page/pageNum/current 参数，会自动替换。
    3. 否则只返回 first_url，避免给该 JS 站点乱拼无效分页。
    """
    if max_pages <= 1:
        return [first_url]

    if "{page}" in first_url:
        return [first_url.format(page=page_no) for page_no in range(1, max_pages + 1)]

    parsed = urlparse(first_url)
    query = parse_qs(parsed.query)
    page_keys = [key for key in ("page", "pageNum", "pageNo", "current") if key in query]

    if not page_keys:
        return [first_url]

    page_key = page_keys[0]
    return [_replace_or_add_query(first_url, **{page_key: str(page_no)}) for page_no in range(1, max_pages + 1)]
