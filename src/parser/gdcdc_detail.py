"""广东省疾病预防控制中心 - 详情页解析模块。"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import clean_text, extract_date


DEFAULT_SOURCE_DEPARTMENT = "广东省疾病预防控制中心"

FILE_EXTENSIONS = (
    "pdf", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "csv", "txt", "zip", "rar", "7z",
    "mp4", "mov", "m4v", "avi", "wmv", "flv", "webm", "m3u8"
)

ATTACHMENT_SUFFIX_RE = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z|mp4|mov|m4v|avi|wmv|flv|webm|m3u8)(?:$|\?|#)",
    re.IGNORECASE,
)

IMAGE_SUFFIX_RE = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|bmp)(?:$|\?|#)",
    re.IGNORECASE,
)


def _safe_text(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = BeautifulSoup(text, "lxml").get_text(" ", strip=True)
    text = clean_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_title(soup: BeautifulSoup) -> str:
    selectors = [
        "div.con h3",
        "div.main div.con h3",
        "h3",
        "h1",
        ".article-title",
        ".title",
        "title",
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue

        title = _safe_text(node.get_text(" ", strip=True))
        title = re.sub(r"_.*?广东省疾病预防控制中心.*$", "", title).strip()

        if title:
            return title

    return ""


def _extract_date_and_source(soup: BeautifulSoup) -> tuple[str, str]:
    """
    详情页常见结构：
        div.ly
          span 信息来源：xxx
          span 发布日期：2026-04-23
    """
    publish_date = ""
    source_department = ""

    ly_node = soup.select_one("div.ly, .ly")
    ly_text = _safe_text(ly_node.get_text(" ", strip=True)) if ly_node else ""

    if ly_text:
        publish_date = extract_date(ly_text)

        source_match = re.search(r"(?:信息来源|来源)\s*[:：]\s*([^\s]+)", ly_text)
        if source_match:
            source_department = _safe_text(source_match.group(1))

    if ly_node:
        for span in ly_node.find_all("span"):
            span_text = _safe_text(span.get_text(" ", strip=True))

            if not publish_date and any(key in span_text for key in ["发布日期", "发布时间", "时间"]):
                publish_date = extract_date(span_text)

            if not source_department and any(key in span_text for key in ["信息来源", "来源"]):
                source_department = re.sub(r"^(信息来源|来源)\s*[:：]\s*", "", span_text).strip()

    page_text = _safe_text(soup.get_text(" ", strip=True))

    if not publish_date:
        date_match = re.search(
            r"(?:发布日期|发布时间|时间)\s*[:：]?\s*((?:20\d{2}|19\d{2})[-./年]\d{1,2}[-./月]\d{1,2}日?)",
            page_text,
        )
        if date_match:
            publish_date = extract_date(date_match.group(1))

    if not publish_date:
        publish_date = extract_date(page_text)

    if not source_department:
        source_match = re.search(r"(?:信息来源|来源)\s*[:：]\s*([^\s]{2,80})", page_text)
        if source_match:
            source_department = _safe_text(source_match.group(1))

    if not source_department:
        source_department = DEFAULT_SOURCE_DEPARTMENT

    return publish_date, source_department


def _find_body_node(soup: BeautifulSoup):
    selectors = [
        "div.article",
        "div.content#articleCon",
        "div#articleCon",
        "div.TRS_Editor",
        "div.trs_editor_view",
        "div.content",
        "div.con",
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue

        text = _safe_text(node.get_text(" ", strip=True))

        if len(text) > 20 or node.find("img") or node.find("a"):
            return node

    candidates = []
    for node in soup.find_all("div"):
        text = _safe_text(node.get_text(" ", strip=True))
        if len(text) > 80:
            candidates.append((len(text), node))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return soup.body or soup


def _infer_file_type(url: str, file_name: str = "", link_text: str = "", title_attr: str = "") -> str:
    candidates = [
        url or "",
        file_name or "",
        link_text or "",
        title_attr or "",
        urlparse(url or "").path,
    ]

    for text in candidates:
        match = ATTACHMENT_SUFFIX_RE.search(text)
        if match:
            return match.group(1).lower()

    joined = " ".join(candidates).lower()
    for ext in FILE_EXTENSIONS:
        if re.search(rf"(?<![a-z0-9]){re.escape(ext)}(?![a-z0-9])", joined):
            return ext

    return "bin"


def _clean_file_name(file_name: str, file_type: str, fallback_prefix: str = "附件") -> str:
    file_name = html_lib.unescape(file_name or "")
    file_name = _safe_text(file_name)
    file_name = file_name.replace("附件：", "").replace("附件:", "")
    file_name = file_name.replace("请点击查看：", "").replace("点击查看：", "")
    file_name = re.sub(r"^[：:\-\s]+", "", file_name).strip()
    file_name = re.sub(r"\s+", " ", file_name)

    if not file_name:
        file_name = f"{fallback_prefix}.{file_type}"

    if file_type and file_type != "bin":
        if not file_name.lower().endswith(f".{file_type}"):
            file_name = f"{file_name}.{file_type}"

    return file_name


def _extract_attachments(html: str, soup: BeautifulSoup, detail_url: str) -> list[dict]:
    attachments = []
    seen_urls = set()

    raw_a_tags = re.finditer(
        r'<a\s+[^>]*href=[\'\"]([^\'\"]+?)[\'\"][^>]*>(.*?)</a>',
        html,
        re.IGNORECASE | re.DOTALL,
    )

    for match in raw_a_tags:
        href = match.group(1).strip()
        inner_html = match.group(2)
        inner_text = _safe_text(re.sub(r"<[^>]+>", " ", inner_html))

        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)

        is_attachment = (
            ATTACHMENT_SUFFIX_RE.search(full_url)
            or ATTACHMENT_SUFFIX_RE.search(inner_text)
            or "/attachment/" in full_url
            or "nfw-cms-attachment" in match.group(0)
        )

        if not is_attachment or full_url in seen_urls:
            continue

        file_type = _infer_file_type(full_url, inner_text, inner_text)
        file_name = _clean_file_name(inner_text or href.rsplit("/", 1)[-1], file_type)

        attachments.append({
            "name": file_name,
            "url": full_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        seen_urls.add(full_url)

    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()

        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)

        text = _safe_text(a_tag.get_text(" ", strip=True))
        title_attr = _safe_text(a_tag.get("title", ""))
        download_attr = _safe_text(a_tag.get("download", ""))
        alt_attr = _safe_text(a_tag.get("alt", ""))
        class_text = " ".join(a_tag.get("class", []))

        is_attachment = (
            ATTACHMENT_SUFFIX_RE.search(full_url)
            or ATTACHMENT_SUFFIX_RE.search(text)
            or ATTACHMENT_SUFFIX_RE.search(title_attr)
            or ATTACHMENT_SUFFIX_RE.search(download_attr)
            or "nfw-cms-attachment" in class_text
            or "/attachment/" in full_url
        )

        if not is_attachment or full_url in seen_urls:
            continue

        file_name_raw = download_attr or title_attr or text or alt_attr or href.rsplit("/", 1)[-1]
        file_type = _infer_file_type(full_url, file_name_raw, text, title_attr)
        file_name = _clean_file_name(file_name_raw, file_type)

        attachments.append({
            "name": file_name,
            "url": full_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        seen_urls.add(full_url)

    for media_tag in soup.find_all(["video", "source", "embed", "object"]):
        src = (
            media_tag.get("src")
            or media_tag.get("data-src")
            or media_tag.get("data-url")
            or media_tag.get("data")
        )

        if not src or src.startswith("data:"):
            continue

        full_url = urljoin(detail_url, src)

        if full_url in seen_urls:
            continue

        match = ATTACHMENT_SUFFIX_RE.search(full_url)
        if not match:
            continue

        file_type = match.group(1).lower()
        file_name = media_tag.get("title") or media_tag.get("alt") or ""
        if not file_name:
            file_name = f"video_{hashlib.md5(full_url.encode()).hexdigest()[:12]}.{file_type}"

        file_name = _clean_file_name(file_name, file_type, fallback_prefix="视频")

        attachments.append({
            "name": file_name,
            "url": full_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        seen_urls.add(full_url)

    return attachments


def _safe_image_ext(src: str) -> str:
    match = IMAGE_SUFFIX_RE.search(src or "")
    if match:
        return match.group(1).lower()
    return "jpg"


def _extract_images(body_node, detail_url: str) -> list[dict]:
    images = []

    if not body_node:
        return images

    seen_urls = set()

    for img in body_node.find_all("img"):
        src = img.get("src") or img.get("data-src")

        if not src:
            continue

        if src.startswith("data:"):
            continue

        full_url = urljoin(detail_url, src)

        if full_url in seen_urls:
            continue

        lower_url = full_url.lower()

        if any(skip in lower_url for skip in ["icon_", "logo", "jiucuo", "red.png"]):
            continue

        ext = _safe_image_ext(src)
        img_name = f"img_{hashlib.md5(full_url.encode()).hexdigest()[:12]}.{ext}"

        images.append({
            "url": full_url,
            "file_name": img_name,
            "local_path": "",
            "download_status": "pending",
        })

        img["src"] = f"images/{img_name}"
        seen_urls.add(full_url)

    return images


def parse_detail_page(html: str, detail_url: str) -> dict:
    """解析广东省疾控中心详情页。"""

    soup_for_extract = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(html, soup_for_extract, detail_url)

    soup = BeautifulSoup(html, "lxml")

    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    title = _extract_title(soup)
    publish_date, source_department = _extract_date_and_source(soup)

    body_node = _find_body_node(soup)
    images = _extract_images(body_node, detail_url)

    body_text = _safe_text(body_node.get_text(" ", strip=True)) if body_node else ""
    body_html = str(body_node) if body_node else ""

    return {
        "title": title,
        "publish_date": publish_date,
        "source_department": source_department,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images,
    }
