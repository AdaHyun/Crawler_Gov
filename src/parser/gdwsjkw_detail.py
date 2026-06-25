"""广东省卫生健康委员会 - 详情页解析模块。"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import clean_text, extract_date


DEFAULT_SOURCE_DEPARTMENT = "广东省卫生健康委员会"

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
    selectors = [
        "div.content-box h1.title",
        "h1.title",
        "div.article h1",
        "h1",
        ".article-title",
        ".content-title",
        ".title",
        "title",
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue

        title = clean_text(node.get_text(" ", strip=True))
        title = re.sub(r"_.*?广东省卫生健康委员会.*$", "", title).strip()
        title = re.sub(r"_.*?国家卫生健康委员会.*$", "", title).strip()

        if title:
            return title

    return ""


def _extract_source(page_text: str) -> str:
    patterns = [
        r"来源\s*[:：]\s*([^\s发布时间发布日期]{2,80})",
        r"来源\s*[:：]\s*(.{2,80}?)(?:\s+|发布时间|发布日期|时间|$)",
        r"发布机构\s*[:：]\s*([^\s]{2,80})",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            source = clean_text(match.group(1))
            if source:
                return source

    return ""


def _extract_meta_from_gkmlpt(soup: BeautifulSoup) -> dict:
    """
    解析规范性文件库详情页顶部元数据表。

    页面结构大致：
        div.classify table tr td label/value
    """
    meta = {}

    table = soup.select_one("div.classify table")
    if not table:
        return meta

    for tr in table.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue

        # 每行可能是：label value label value
        for i in range(0, len(tds) - 1, 2):
            key = clean_text(tds[i].get_text(" ", strip=True))
            value = clean_text(tds[i + 1].get_text(" ", strip=True))

            key = key.replace("：", "").replace(":", "").strip()

            if key and value:
                meta[key] = value

    return meta


def _extract_date_and_source(soup: BeautifulSoup, page_text: str) -> tuple[str, str]:
    publish_date = ""
    source_department = ""

    # 1. 规范性文件库元数据表
    meta = _extract_meta_from_gkmlpt(soup)

    if meta:
        publish_date = (
            extract_date(meta.get("发布日期", ""))
            or extract_date(meta.get("成文日期", ""))
        )
        source_department = meta.get("发布机构", "")

    # 2. 普通广东卫健委详情页：p.text_center
    if not publish_date or not source_department:
        info_node = soup.select_one("div.article p.text_center, p.text_center, .date-row")
        info_text = clean_text(info_node.get_text(" ", strip=True)) if info_node else ""

        if info_text:
            if not publish_date:
                publish_date = extract_date(info_text)

            if not source_department:
                source_match = re.search(r"来源\s*[:：]\s*([^\s]+)", info_text)
                if source_match:
                    source_department = clean_text(source_match.group(1))

    # 3. 全文兜底
    if not publish_date:
        date_match = re.search(
            r"(?:发布时间|发布日期|时间|日期)\s*[:：]?\s*((?:20\d{2}|19\d{2})[-./年]\d{1,2}[-./月]\d{1,2}日?)",
            page_text,
        )
        if date_match:
            publish_date = extract_date(date_match.group(1))

    if not publish_date:
        publish_date = extract_date(page_text)

    if not source_department:
        source_department = _extract_source(page_text)

    if not source_department:
        source_department = DEFAULT_SOURCE_DEPARTMENT

    return publish_date, source_department


def _find_body_node(soup: BeautifulSoup):
    """
    寻找正文区域。

    广东卫健委普通详情：
        div.content-content

    规范性文件库：
        div.article-content

    国家文件外链 NHC：
        div.TRS_Editor / div#UCAP-CONTENT 等
    """
    selectors = [
        "div.content-content",
        "div.content-box div.article-content",
        "div.article-content",
        "div.TRS_Editor",
        "div.trs_editor_view",
        "div#UCAP-CONTENT",
        "div#zoom",
        "div#xw_box",
        "div.Custom_UnionStyle",
        "div.pages_content",
        "div.article_content",
        "div.content",
    ]

    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue

        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 20 or node.find("img") or node.find("a"):
            return node

    # 兜底：找文本最长的 div
    candidates = []
    for node in soup.find_all("div"):
        text = clean_text(node.get_text(" ", strip=True))
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

    # 不返回 unknown，避免 main.py 生成 .unknown
    return "bin"


def _clean_file_name(file_name: str, file_type: str, fallback_prefix: str = "附件") -> str:
    file_name = html_lib.unescape(file_name or "")
    file_name = clean_text(file_name)
    file_name = file_name.replace("请点击查看：", "").replace("点击查看：", "").strip()
    file_name = re.sub(r"^[：:\-\s]+", "", file_name)
    file_name = re.sub(r"\s+", " ", file_name).strip()

    if not file_name:
        file_name = f"{fallback_prefix}.{file_type}"

    if file_type and file_type != "bin":
        if not file_name.lower().endswith(f".{file_type}"):
            file_name = f"{file_name}.{file_type}"

    return file_name


def _extract_attachments(html: str, soup: BeautifulSoup, detail_url: str) -> list[dict]:
    """
    提取附件。

    支持：
    1. URL 直接带 .pdf/.doc/.xlsx
    2. a 标签 class=nfw-cms-attachment
    3. 链接文字里带 .pdf/.doc/.xlsx
    4. 原始 HTML 中隐藏的 a 标签
    """
    attachments = []
    seen_urls = set()

    # 1. 原始 HTML 扫描
    raw_a_tags = re.finditer(
        r'<a\s+[^>]*href=[\'"]([^\'"]+?)[\'"][^>]*>(.*?)</a>',
        html,
        re.IGNORECASE | re.DOTALL,
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
            or "/attachment/" in full_url
            or "nfw-cms-attachment" in inner_html
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

    # 2. BeautifulSoup 全局扫描
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()

        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)

        text = clean_text(a_tag.get_text(" ", strip=True))
        title_attr = clean_text(a_tag.get("title", ""))
        class_text = " ".join(a_tag.get("class", []))

        is_attachment = (
            ATTACHMENT_SUFFIX_RE.search(full_url)
            or ATTACHMENT_SUFFIX_RE.search(text)
            or ATTACHMENT_SUFFIX_RE.search(title_attr)
            or "nfw-cms-attachment" in class_text
            or "/attachment/" in full_url
        )

        if not is_attachment or full_url in seen_urls:
            continue

        file_name_raw = title_attr or text or href.rsplit("/", 1)[-1]
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
        src = img.get("src") or img.get("data-src")

        if not src:
            continue

        if src.startswith("data:"):
            continue

        full_url = urljoin(detail_url, src)

        # 跳过明显的站点图标、分享图标
        lower_url = full_url.lower()
        if any(skip in lower_url for skip in ["icon_", "logo", "qrcode", "jiucuo", "red.png"]):
            # 正文中确实可能有二维码，但一般不是正文图片，先跳过
            continue

        ext = _safe_image_ext(src)
        img_name = f"img_{hashlib.md5(full_url.encode()).hexdigest()[:12]}.{ext}"

        images.append({
            "url": full_url,
            "file_name": img_name,
            "local_path": "",
            "download_status": "pending",
        })

        # 本地化换链
        img["src"] = f"images/{img_name}"

    return images


def parse_detail_page(html: str, detail_url: str) -> dict:
    """解析详情页标题、日期、来源、正文、附件和图片。"""

    # 附件先从未清洗的原始 HTML 中提取，防止漏掉
    soup_for_extract = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(html, soup_for_extract, detail_url)

    soup = BeautifulSoup(html, "lxml")

    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    body_node = _find_body_node(soup)

    title = _extract_title(soup)
    page_text = clean_text(soup.get_text(" ", strip=True))
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
        "images": images,
    }