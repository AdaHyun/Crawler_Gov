"""中国疾病预防控制中心 - 详情页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin
import hashlib  # 新增

from bs4 import BeautifulSoup

from utils import clean_text, extract_date

ATTACHMENT_SUFFIX_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|zip)(?:$|\?)", re.IGNORECASE)


def _extract_title(soup: BeautifulSoup) -> str:
    selectors = [
        "div.left h5",
        ".title", 
        "h1",
        ".article-title"
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
    source_department = "中国疾病预防控制中心" 

    xq_con = soup.select_one(".xqCon")
    if xq_con:
        xq_text = clean_text(xq_con.get_text(" ", strip=True))
        publish_date = extract_date(xq_text)
        source_match = re.search(r"(?:来源|供稿)\s*[:：]\s*([^\s]{2,20})", xq_text)
        if source_match:
            source_department = clean_text(source_match.group(1))

    if not publish_date:
        publish_date = extract_date(page_text)

    return publish_date, source_department


def _find_body_node(soup: BeautifulSoup):
    selectors = [
        "div#articleCon",
        "div.trs_editor_view",
        "div.Custom_UnionStyle",
        "div.content",
        "div.article_content"
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 20:
            return node
    return soup.body or soup


def parse_detail_page(html: str, detail_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    body_node = _find_body_node(soup)

    # ================= 新增：图片提取与本地化换链逻辑 =================
    images = []
    if body_node:
        for img in body_node.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            
            full_url = urljoin(detail_url, src)
            ext = src.split('.')[-1].split('?')[0]
            if len(ext) > 5 or not ext: 
                ext = "jpg"
                
            img_name = f"img_{hashlib.md5(full_url.encode()).hexdigest()[:12]}.{ext}"
            
            images.append({
                "url": full_url,
                "file_name": img_name,
                "local_path": "",
                "download_status": "pending"
            })
            
            img["src"] = f"images/{img_name}" 
    # ==================================================================

    page_text = clean_text(soup.get_text(" ", strip=True))
    body_text = clean_text(body_node.get_text(" ", strip=True)) if body_node else ""
    body_html = str(body_node) if body_node else ""

    publish_date, source_department = _extract_date_and_source(soup, page_text)

    attachments = []
    seen_urls = set()
    for a_tag in body_node.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        full_url = urljoin(detail_url, href)
        match = ATTACHMENT_SUFFIX_RE.search(full_url)
        if match and full_url not in seen_urls:
            attachments.append({
                "name": clean_text(a_tag.get_text(" ", strip=True)) or href.split("/")[-1],
                "url": full_url,
                "file_type": match.group(1).lower(),
                "local_path": "",               # 新增同步字段
                "download_status": "pending"    # 新增同步字段
            })
            seen_urls.add(full_url)

    return {
        "title": _extract_title(soup),
        "publish_date": publish_date,
        "source_department": source_department,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images  # 新增同步字段
    }