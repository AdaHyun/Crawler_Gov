"""中国老龄协会官网（银龄在线）- 健康科普详情页解析模块。

示例详情页：
    https://www.cncaprc.gov.cn/jkkp1njk/771984.jhtml

保存路径：
    src/parser/cncaprc_detail.py
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import clean_text, extract_date


ATTACHMENT_SUFFIX_RE = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?)",
    re.IGNORECASE,
)
IMAGE_SUFFIX_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp|bmp|svg)(?:$|\?)", re.IGNORECASE)


def _extract_title(soup: BeautifulSoup) -> str:
    """优先从详情页 h1.article-title 提取标题。"""
    selectors = [
        "h1.article-title",
        ".article-title",
        "header.article-header h1",
        "h1",
        "title",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        title = clean_text(node.get_text(" ", strip=True))
        title = re.sub(r"[_\-—|].*?(银龄在线|中国老龄协会).*$", "", title).strip()
        if title:
            return title
    return ""


def _extract_source(page_text: str, meta_text: str = "") -> str:
    """从元信息中提取来源。"""
    text = clean_text(meta_text or page_text)

    patterns = [
        r"来源\s*[:：]\s*(.*?)(?:\s+日期\s*[:：]|\s+发布时间\s*[:：]|\s+发布日期\s*[:：]|\s+(?:19|20)\d{2}[-./年]|$)",
        r"来源\s*[:：]\s*([^\s]{2,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            source = clean_text(match.group(1))
            source = re.sub(r"日期\s*[:：].*$", "", source).strip()
            if source:
                return source
    return ""


def _extract_publish_date(page_text: str, meta_text: str = "") -> str:
    """从元信息或正文全文中提取发布日期。"""
    text = clean_text(meta_text or page_text)

    patterns = [
        r"日期\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
        r"发布时间\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
        r"发布日期\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            publish_date = extract_date(match.group(1))
            if publish_date:
                return publish_date
    return extract_date(page_text)


def _find_body_node(soup: BeautifulSoup):
    """寻找正文区域，避开导航和页脚。"""
    selectors = [
        "article.article-content",
        ".article-content",
        "main.article-page article",
        ".article-page-container article",
        "div.TRS_Editor",
        "div.content",
        "div.article",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 20:
            return node

    # 兜底：选择文本最长的 main/div 节点。
    candidates = []
    for node in soup.select("main, article, div"):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 80:
            candidates.append((len(text), node))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return soup.body or soup


def _file_type_from_name_or_url(name: str, url: str = "") -> str:
    """根据文件名或 URL 推断附件类型。"""
    for value in (name, url):
        match = ATTACHMENT_SUFFIX_RE.search(value or "")
        if match:
            return match.group(1).lower()
    if "." in name:
        suffix = name.rsplit(".", 1)[-1].lower()
        if 1 <= len(suffix) <= 5:
            return suffix
    return "unknown"


def _extract_attachments(html: str, soup: BeautifulSoup, detail_url: str) -> list[dict]:
    """提取正文中的附件直链。"""
    attachments: list[dict] = []
    seen_urls: set[str] = set()

    # 1. BeautifulSoup 扫描所有 a 标签。
    for a_tag in soup.find_all("a", href=True):
        href = (a_tag.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue

        full_url = urljoin(detail_url, href)
        text = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
        match = ATTACHMENT_SUFFIX_RE.search(full_url)
        text_has_suffix = ATTACHMENT_SUFFIX_RE.search(text or "")
        if not (match or text_has_suffix):
            continue
        if full_url in seen_urls:
            continue

        name = text or PurePosixPath(urlparse(full_url).path).name or "未命名附件"
        file_type = _file_type_from_name_or_url(name, full_url)
        attachments.append({
            "name": name,
            "url": full_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        seen_urls.add(full_url)

    # 2. 原始 HTML 兜底，防止附件在特殊结构里没被 soup 正常识别。
    raw_a_tags = re.finditer(r"<a\s+[^>]*href=[\'\"]([^\'\"]+?)[\'\"][^>]*>(.*?)</a>", html, re.IGNORECASE | re.DOTALL)
    for match in raw_a_tags:
        href = match.group(1).strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        full_url = urljoin(detail_url, href)
        if full_url in seen_urls or not ATTACHMENT_SUFFIX_RE.search(full_url):
            continue
        inner_text = clean_text(re.sub(r"<[^>]+>", " ", match.group(2)))
        name = inner_text or PurePosixPath(urlparse(full_url).path).name or "未命名附件"
        file_type = _file_type_from_name_or_url(name, full_url)
        attachments.append({
            "name": name,
            "url": full_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        seen_urls.add(full_url)

    return attachments


def _image_ext_from_url(url: str) -> str:
    """从图片 URL 推断扩展名。"""
    path = urlparse(url).path
    match = IMAGE_SUFFIX_RE.search(path)
    if match:
        ext = match.group(1).lower()
        return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _extract_images(body_node, detail_url: str) -> list[dict]:
    """提取正文图片，并把 body_html 中的图片地址替换为相对路径。"""
    images: list[dict] = []
    seen_urls: set[str] = set()

    if not body_node:
        return images

    for img in body_node.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            continue

        full_url = urljoin(detail_url, src)
        if full_url in seen_urls:
            continue

        ext = _image_ext_from_url(full_url)
        img_name = f"img_{hashlib.md5(full_url.encode('utf-8')).hexdigest()[:12]}.{ext}"

        images.append({
            "url": full_url,
            "file_name": img_name,
            "local_path": "",
            "download_status": "pending",
        })
        seen_urls.add(full_url)

        # main.py 会把图片下载到文章目录；正文 HTML 里保留相对引用。
        img["src"] = f"images/{img_name}"

    return images


def parse_detail_page(html: str, detail_url: str) -> dict:
    """解析详情页标题、日期、来源、正文、附件和正文图片。"""
    # 附件先用未清理的 soup 解析，避免误删隐藏链接。
    soup_for_assets = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(html, soup_for_assets, detail_url)

    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    meta_node = soup.select_one(".article-meta, header.article-header .article-meta, .meta, .info")
    meta_text = clean_text(meta_node.get_text(" ", strip=True)) if meta_node else ""

    body_node = _find_body_node(soup)
    images = _extract_images(body_node, detail_url)

    page_text = clean_text(soup.get_text(" ", strip=True))
    body_text = clean_text(body_node.get_text(" ", strip=True)) if body_node else ""
    body_html = str(body_node) if body_node else ""

    return {
        "title": _extract_title(soup),
        "publish_date": _extract_publish_date(page_text, meta_text),
        "source_department": _extract_source(page_text, meta_text),
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images,
    }
