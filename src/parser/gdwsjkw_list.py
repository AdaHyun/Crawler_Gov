"""广东省卫生健康委员会 - 列表页解析模块。

支持栏目：
1. 公卫信息
2. 国家文件
3. 卫生健康规划计划
4. 数据发布
5. 广东省卫生健康委员会规范性文件库
6. 政策解读
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


FILE_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".csv", ".txt", ".zip", ".rar", ".7z"
)

INVALID_TITLE_KEYWORDS = (
    "首页", "上一页", "下一页", "末页", "尾页", "更多", "返回",
    "第一 页", "第一页", "最后一页", "下一页", "上一页"
)

SESSION = requests.Session()

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """
    构造列表页 URL。

    普通静态栏目：
        index.html
        index_2.html
        index_3.html

    规范性文件库、政策解读：
        这两个栏目通过接口在 parse_list_page() 内部翻页，
        所以这里只返回第一页入口 URL。
    """
    if max_pages <= 1:
        return [first_url]

    # 接口型栏目，parse_list_page 内部会自己翻页
    if "gkmlpt/search" in first_url or "hdjlpt/c_cat" in first_url:
        return [first_url]

    page_urls = [first_url]

    if first_url.endswith("index.html"):
        prefix = first_url[:-len("index.html")]
        for page_no in range(2, max_pages + 1):
            page_urls.append(f"{prefix}index_{page_no}.html")
        return page_urls

    return [first_url]


def _safe_title(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = clean_text(BeautifulSoup(text, "lxml").get_text(" ", strip=True))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _timestamp_to_date(value) -> str:
    """把接口里的时间戳转成 YYYY-MM-DD。兼容秒级/毫秒级。"""
    if value in ("", None, 0, "0"):
        return ""

    try:
        ts = float(value)
    except (TypeError, ValueError):
        return extract_date(str(value))

    # 毫秒级时间戳
    if ts > 10_000_000_000:
        ts = ts / 1000

    # 广东站点显示为北京时间，使用 UTC+8 转换，避免日期差一天
    try:
        return (datetime.utcfromtimestamp(ts) + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _same_domain_or_allowed(url: str, site_domain: str, allow_external: bool = False) -> bool:
    parsed = urlparse(url)

    if not parsed.netloc:
        return True

    if site_domain in parsed.netloc:
        return True

    # 国家文件栏目会出现国家卫健委外链
    if allow_external:
        return True

    return False


def _is_valid_url(url: str, site_domain: str, allow_external: bool = False) -> bool:
    if not url:
        return False

    lower_url = url.lower()

    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    if not _same_domain_or_allowed(url, site_domain, allow_external):
        return False

    parsed = urlparse(url)

    # 普通文章
    if "/content/post_" in parsed.path:
        return True

    # 规范性文件库详情
    if "/gkmlpt/content/" in parsed.path:
        return True

    # 外链国家文件
    if allow_external and parsed.path.endswith((".html", ".htm", ".shtml")):
        return True

    # 直链文件
    if lower_url.split("?")[0].endswith(FILE_SUFFIXES):
        return True

    return False


def _infer_file_type(url: str, name: str = "") -> str:
    candidates = [url or "", name or ""]
    for text in candidates:
        match = re.search(
            r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?|#)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).lower()
    return "bin"


def _extract_id_from_url(url: str, fallback: int) -> int:
    patterns = [
        r"post_(\d+)",
        r"/content/\d+/\d+/post_(\d+)",
        r"/content/post_(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return int(match.group(1))

    return fallback


def _build_item(
    *,
    title: str,
    detail_url: str,
    publish_date: str,
    channel: dict,
    site_config: dict,
    index: int,
    source_department: str = "",
    raw_extra: dict | None = None,
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

    if source_department:
        item["organization"]["source_department"] = source_department

    item["raw"]["raw_title"] = title
    item["raw"]["raw_date"] = publish_date
    item["raw"]["raw_source"] = source_department

    if raw_extra:
        item["raw"].update(raw_extra)

    clean_url = detail_url.lower().split("?")[0]

    if clean_url.endswith(FILE_SUFFIXES):
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


def _parse_static_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """
    解析普通静态列表页：
        公卫信息
        国家文件
        卫生健康规划计划
        数据发布
    """
    soup = BeautifulSoup(html, "lxml")

    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))
    channel_name = channel.get("channel_name", "")

    allow_external = channel_name == "国家文件"

    records: list[dict] = []
    seen_urls: set[str] = set()

    candidate_nodes = soup.select("div.article div.section.list ul li")
    if not candidate_nodes:
        candidate_nodes = soup.select("div.section.list li, div.article li, li")

    for node in candidate_nodes:
        a_tag = node.find("a", href=True)
        if not a_tag:
            continue

        title = _safe_title(a_tag.get("title", "")) or _safe_title(a_tag.get_text(" ", strip=True))

        if not title or title in INVALID_TITLE_KEYWORDS or len(title) < 4:
            continue

        if "第一页" in title or "上一页" in title or "下一页" in title or "最后一页" in title:
            continue

        href = a_tag.get("href", "").strip()
        detail_url = urljoin(base_url, href)

        if detail_url in seen_urls:
            continue

        if not _is_valid_url(detail_url, site_domain, allow_external=allow_external):
            continue

        node_text = clean_text(node.get_text(" ", strip=True))
        publish_date = extract_date(node_text)

        if not publish_date:
            span = node.find("span")
            if span:
                publish_date = extract_date(span.get_text(" ", strip=True))

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


def _parse_gkmlpt_api(channel: dict, site_config: dict) -> list[dict]:
    """
    解析广东省卫健委规范性文件库接口。

    接口：
        GET https://wsjkw.gd.gov.cn/gkmlpt/api/all/4196?page=1&sid=216
    """
    records: list[dict] = []
    seen_urls: set[str] = set()

    max_pages = int(site_config.get("max_pages", 20))
    api_tpl = "https://wsjkw.gd.gov.cn/gkmlpt/api/all/4196?page={page}&sid=216"

    headers = dict(REQUEST_HEADERS)
    headers["Referer"] = channel.get("channel_url", "https://wsjkw.gd.gov.cn/gkmlpt/search?type=standardSearch")

    # 先访问入口页，拿 session cookie
    try:
        SESSION.get(channel.get("channel_url", ""), headers=headers, timeout=20)
    except Exception:
        pass

    for page_no in range(1, max_pages + 1):
        api_url = api_tpl.format(page=page_no)

        try:
            resp = SESSION.get(api_url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break

        articles = data.get("articles") or data.get("data", {}).get("articles") or []
        if not articles:
            break

        for article in articles:
            title = _safe_title(article.get("title", ""))
            detail_url = html_lib.unescape(article.get("url", "") or "")

            if not title or not detail_url:
                continue

            if detail_url in seen_urls:
                continue

            publish_date = (
                _timestamp_to_date(article.get("date"))
                or _timestamp_to_date(article.get("created_at"))
                or _timestamp_to_date(article.get("published_at"))
            )

            publisher = _safe_title(article.get("publisher", ""))

            raw_extra = {
                "document_number": _safe_title(article.get("document_number", "")),
                "identifier": _safe_title(article.get("identifier", "")),
                "identifier_f": _safe_title(article.get("identifier_f", "")),
                "identifier_b": _safe_title(article.get("identifier_b", "")),
                "file_status": "现行有效" if str(article.get("is_expired", "")) == "0" else "",
                "gkmlpt_id": article.get("id", ""),
                "gkmlpt_page": page_no,
            }

            item = _build_item(
                title=title,
                detail_url=detail_url,
                publish_date=publish_date,
                channel=channel,
                site_config=site_config,
                index=int(article.get("id") or len(records) + 1),
                source_department=publisher,
                raw_extra=raw_extra,
            )

            records.append(item)
            seen_urls.add(detail_url)

    return records


def _parse_policy_interpretation_api(channel: dict, site_config: dict) -> list[dict]:
    """
    解析政策解读接口。

    接口：
        POST https://wsjkw.gd.gov.cn/hdjlpt/letter/cms/classify/articles

    表单：
        offset = 0, 20, 40...
        limit = 20
        classify = ZCJD
    """
    records: list[dict] = []
    seen_urls: set[str] = set()

    max_pages = int(site_config.get("max_pages", 20))
    limit = 20

    api_url = "https://wsjkw.gd.gov.cn/hdjlpt/letter/cms/classify/articles"

    headers = dict(REQUEST_HEADERS)
    headers.update({
        "Referer": channel.get("channel_url", "https://wsjkw.gd.gov.cn/hdjlpt/c_cat?name=zcjd&via=pc"),
        "Origin": "https://wsjkw.gd.gov.cn",
        "X-Requested-With": "XMLHttpRequest",
    })

    # 先访问入口页，拿 session cookie
    try:
        SESSION.get(channel.get("channel_url", ""), headers=headers, timeout=20)
    except Exception:
        pass

    total = None

    for page_no in range(1, max_pages + 1):
        offset = (page_no - 1) * limit

        form_data = {
            "offset": offset,
            "limit": limit,
            "classify": "ZCJD",
        }

        try:
            resp = SESSION.post(api_url, headers=headers, data=form_data, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break

        if str(data.get("errcode", "0")) not in ("0", ""):
            break

        payload = data.get("data", {}) or {}
        articles = payload.get("list", []) or []

        if total is None:
            total = payload.get("total")

        if not articles:
            break

        for article in articles:
            title = _safe_title(article.get("title", ""))
            detail_url = (
                article.get("post_url")
                or article.get("url")
                or ""
            )
            detail_url = html_lib.unescape(detail_url)

            if not title or not detail_url:
                continue

            if detail_url in seen_urls:
                continue

            publish_date = (
                _timestamp_to_date(article.get("display_publish_time"))
                or _timestamp_to_date(article.get("publish_time"))
                or _timestamp_to_date(article.get("first_publish_time"))
            )

            raw_extra = {
                "policy_interpretation_id": article.get("id", ""),
                "api_offset": offset,
                "api_page": page_no,
                "raw_summary": _safe_title(article.get("content", "")),
            }

            item = _build_item(
                title=title,
                detail_url=detail_url,
                publish_date=publish_date,
                channel=channel,
                site_config=site_config,
                index=int(article.get("id") or len(records) + 1),
                source_department="广东省卫生健康委员会",
                raw_extra=raw_extra,
            )

            records.append(item)
            seen_urls.add(detail_url)

        if total is not None and offset + limit >= int(total):
            break

    return records


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """根据栏目类型分发到不同列表解析逻辑。"""
    channel_url = channel.get("channel_url", "")

    if "gkmlpt/search" in channel_url:
        return _parse_gkmlpt_api(channel, site_config)

    if "hdjlpt/c_cat" in channel_url:
        return _parse_policy_interpretation_api(channel, site_config)

    return _parse_static_list_page(html, channel, site_config)