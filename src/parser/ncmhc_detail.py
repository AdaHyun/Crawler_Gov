"""国家心理健康和精神卫生防治中心 - 详情页解析模块。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import clean_text, extract_date


DEFAULT_SOURCE_DEPARTMENT = "国家心理健康和精神卫生防治中心"

FILE_EXTENSIONS = (
    "pdf", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "csv", "txt", "zip", "rar", "7z"
)

ATTACHMENT_SUFFIX_RE = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?|#)",
    re.IGNORECASE
)

IMAGE_SUFFIX_RE = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|bmp)(?:$|\?|#)",
    re.IGNORECASE
)


def _extract_title(soup: BeautifulSoup) -> str:
    title_box = soup.select_one(".content_title_box")
    if title_box:
        # 避免把日期来源也拼进标题
        time_node = title_box.select_one(".content_time")
        if time_node:
            time_node.extract()

        title = clean_text(title_box.get_text(" ", strip=True))
        if title:
            return title

    selectors = [
        "h1",
        ".title",
        ".article-title",
        ".content-title",
        ".detail-title"
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            title = clean_text(node.get_text(" ", strip=True))
            if title:
                return title

    return ""


def _extract_date_and_source(soup: BeautifulSoup, page_text: str) -> tuple[str, str]:
    publish_date = ""
    source_department = DEFAULT_SOURCE_DEPARTMENT

    info_node = soup.select_one(".content_title_box .content_time, .content_time")
    info_text = clean_text(info_node.get_text(" ", strip=True)) if info_node else ""

    if info_text:
        publish_date = extract_date(info_text)

        source_match = re.search(r"(?:来源|信息来源)\s*[:：]?\s*([^\s]+)", info_text)
        if source_match:
            source_department = clean_text(source_match.group(1))

    if not publish_date:
        publish_date = extract_date(page_text)

    if source_department == DEFAULT_SOURCE_DEPARTMENT:
        source_match = re.search(r"(?:来源|信息来源)\s*[:：]?\s*([^\s]+)", page_text)
        if source_match:
            source_department = clean_text(source_match.group(1))

    return publish_date, source_department


def _find_body_node(soup: BeautifulSoup):
    selectors = [
        ".content_end_box",
        ".ultimate_box .content_end_box",
        ".article-content",
        ".detail-content",
        ".content",
        ".TRS_Editor",
        ".trs_editor_view"
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 20:
            return node

    return soup.body or soup


def _infer_file_type(url: str, file_name: str, link_text: str = "", title_attr: str = "") -> str:
    """
    推断附件类型，避免出现 unknown。

    重点处理国家心理健康中心这种链接：
    href=/channel/downfile/xxxx
    文本=1.xxx.pdf
    """
    candidates = [
        url or "",
        file_name or "",
        link_text or "",
        title_attr or "",
        urlparse(url or "").path
    ]

    for text in candidates:
        match = ATTACHMENT_SUFFIX_RE.search(text)
        if match:
            return match.group(1).lower()

    # 有些文本可能是 “附件：xxx pdf” 或 “PDF下载”
    joined = " ".join(candidates).lower()
    for ext in FILE_EXTENSIONS:
        if re.search(rf"(?<![a-z0-9]){re.escape(ext)}(?![a-z0-9])", joined):
            return ext

    # 该站 /channel/downfile/ 基本是附件下载链接。
    # 如果完全判断不出来，先给 bin，不给 unknown，避免 main.py 生成 .unknown。
    if "/channel/downfile/" in url:
        return "bin"

    return "bin"


def _clean_file_name(file_name: str, file_type: str, fallback_prefix: str = "附件") -> str:
    file_name = clean_text(file_name)
    file_name = file_name.replace("下载附件文件", "").strip()
    file_name = re.sub(r"^[：:\-\s]+", "", file_name)

    if not file_name:
        file_name = f"{fallback_prefix}.{file_type}"

    # 去掉浏览器按钮文字、空白等噪声
    file_name = re.sub(r"\s+", " ", file_name).strip()

    # 如果文件名没有后缀，补上推断出来的后缀
    if file_type and file_type != "bin":
        if not file_name.lower().endswith(f".{file_type}"):
            file_name = f"{file_name}.{file_type}"

    return file_name


def _extract_attachments(html: str, soup: BeautifulSoup, detail_url: str) -> list[dict]:
    attachments = []
    seen_urls = set()

    # 1. 先从原始 HTML 抢救，防止 BeautifulSoup 清洗后漏掉
    raw_a_tags = re.finditer(
        r'<a\s+[^>]*href=[\'"]([^\'"]+?)[\'"][^>]*>(.*?)</a>',
        html,
        re.IGNORECASE | re.DOTALL
    )

    for match in raw_a_tags:
        href = match.group(1).strip()
        inner_html = match.group(2)
        inner_text = clean_text(re.sub(r"<[^>]+>", " ", inner_html))

        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)

        is_attachment = (
            ATTACHMENT_SUFFIX_RE.search(full_url)
            or ATTACHMENT_SUFFIX_RE.search(inner_text)
            or "/channel/downfile/" in full_url
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
            "download_status": "pending"
        })
        seen_urls.add(full_url)

    # 2. 再用 soup 全局扫描 a 标签
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()

        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)

        text = clean_text(a_tag.get_text(" ", strip=True))
        title_attr = clean_text(a_tag.get("title", ""))
        file_name_raw = title_attr or text or href.rsplit("/", 1)[-1]

        is_attachment = (
            ATTACHMENT_SUFFIX_RE.search(full_url)
            or ATTACHMENT_SUFFIX_RE.search(text)
            or ATTACHMENT_SUFFIX_RE.search(title_attr)
            or "/channel/downfile/" in full_url
        )

        if not is_attachment or full_url in seen_urls:
            continue

        file_type = _infer_file_type(full_url, file_name_raw, text, title_attr)
        file_name = _clean_file_name(file_name_raw, file_type)

        attachments.append({
            "name": file_name,
            "url": full_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending"
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

    for img in body_node.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        full_url = urljoin(detail_url, src)

        # 跳过下载按钮图标，避免把 /images/download.png 当正文图片
        if "download" in full_url.lower() and "/images/" in full_url.lower():
            continue

        ext = _safe_image_ext(src)
        img_name = f"img_{hashlib.md5(full_url.encode()).hexdigest()[:12]}.{ext}"

        images.append({
            "url": full_url,
            "file_name": img_name,
            "local_path": "",
            "download_status": "pending"
        })

        img["src"] = f"images/{img_name}"

    return images


def parse_detail_page(html: str, detail_url: str) -> dict:
    """
    解析详情页标题、日期、来源、正文、附件和图片。

    注意：
    附件提取必须在清理 script/style/iframe 之前做；
    图片只从正文区域提取。
    """
    soup_for_extract = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(html, soup_for_extract, detail_url)

    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    body_node = _find_body_node(soup)
    page_text = clean_text(soup.get_text(" ", strip=True))

    title = _extract_title(soup)
    publish_date, source_department = _extract_date_and_source(soup, page_text)

    images = _extract_images(body_node, detail_url)

    body_text = clean_text(body_node.get_text(" ", strip=True)) if body_node else ""
    body_html = str(body_node) if body_node else ""

    return {
        "title": title,
        "publish_date": publish_date,
        "source_department": source_department,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images
    }