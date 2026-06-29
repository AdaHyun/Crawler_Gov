"""国家中医药管理局官网 - 详情页解析模块。

适用栏目：
    - 政策文件
    - 政策解读
    - 通知公告

示例详情页：
    http://www.natcm.gov.cn/renjiaosi/zhengcewenjian/2024-01-26/33151.html
    http://www.natcm.gov.cn/bangongshi/gongzuodongtai/2024-07-31/34579.html

保存路径：
    src/parser/natcm_detail.py
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


def _clean_title(title: str) -> str:
    """清理标题中的站点后缀和多余空白。"""
    title = clean_text(title)
    title = re.sub(r"[_\-—|].*?(国家中医药管理局|中医药管理局).*$", "", title).strip()
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _extract_title(soup: BeautifulSoup) -> str:
    """提取详情页标题，兼容国家中医药管理局老 table 模板。"""
    selectors = [
        "span.nrbt",
        "span.bt",
        ".article-title",
        ".title",
        "h1",
        "title",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            title = _clean_title(node.get_text(" ", strip=True))
            if 5 <= len(title) <= 180 and not re.search(r"^(时间|来源|附件|相关链接)[:：]", title):
                return title

    # 兜底：寻找居中且文本长度像标题的节点。
    candidates: list[tuple[int, str]] = []
    for node in soup.find_all(["td", "div", "p", "span"]):
        align = (node.get("align") or "").lower()
        style = (node.get("style") or "").lower()
        cls = " ".join(node.get("class") or [])
        if align != "center" and "text-align:center" not in style.replace(" ", "") and "bt" not in cls:
            continue
        text = _clean_title(node.get_text(" ", strip=True))
        if 8 <= len(text) <= 180 and not re.search(r"时间|来源|当前位置|附件", text):
            candidates.append((len(text), text))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    return ""


def _extract_source(page_text: str) -> str:
    """从页面元信息中提取来源部门。"""
    patterns = [
        r"来源\s*[:：]\s*(.*?)(?:\s+时间\s*[:：]|\s+发布日期\s*[:：]|\s+发布时间\s*[:：]|\s+(?:19|20)\d{2}[-./年]|$)",
        r"来源\s*[:：]\s*([^\s]{2,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text)
        if not match:
            continue
        source = clean_text(match.group(1))
        source = re.sub(r"时间\s*[:：].*$", "", source).strip()
        if source:
            return source
    return ""


def _extract_publish_date(page_text: str) -> str:
    """从“时间/发布日期/发布时间”中提取发布日期。"""
    patterns = [
        r"时间\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?(?:\s+\d{1,2}:\d{1,2}(?::\d{1,2})?)?)",
        r"发布时间\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
        r"发布日期\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            publish_date = extract_date(match.group(1))
            if publish_date:
                return publish_date
    return extract_date(page_text)


def _find_body_node(soup: BeautifulSoup):
    """寻找正文区域。该站详情页正文通常在 span.zw 中。"""
    selectors = [
        "span.zw",
        "td span.zw",
        "div.TRS_Editor",
        "div#zoom",
        "div#Zoom",
        ".article-content",
        ".content",
    ]
    for selector in selectors:
        nodes = soup.select(selector)
        if not nodes:
            continue
        # 同一个 selector 可能有多个节点，选正文最长的。
        candidates = []
        for node in nodes:
            text = clean_text(node.get_text(" ", strip=True))
            if len(text) > 20:
                candidates.append((len(text), node))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

    # 兜底：在老 table 布局里选文本最长的 td/table/div。
    candidates = []
    for node in soup.find_all(["td", "table", "div"]):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 100:
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
    """提取正文附件。"""
    attachments: list[dict] = []
    seen_urls: set[str] = set()

    # 1. BeautifulSoup 扫描 a 标签。
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

    # 2. 原始 HTML 兜底。
    raw_a_tags = re.finditer(
        r"<a\s+[^>]*href=[\'\"]([^\'\"]+?)[\'\"][^>]*>(.*?)</a>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
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
    match = IMAGE_SUFFIX_RE.search(urlparse(url).path)
    if match:
        ext = match.group(1).lower()
        return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _extract_images(body_node, detail_url: str) -> list[dict]:
    """提取正文图片，并把 body_html 中图片地址替换为本地相对路径。"""
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

    body_node = _find_body_node(soup)
    images = _extract_images(body_node, detail_url)

    page_text = clean_text(soup.get_text(" ", strip=True))
    body_text = clean_text(body_node.get_text(" ", strip=True)) if body_node else ""
    body_html = str(body_node) if body_node else ""

    return {
        "title": _extract_title(soup),
        "publish_date": _extract_publish_date(page_text),
        "source_department": _extract_source(page_text),
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images,
    }
