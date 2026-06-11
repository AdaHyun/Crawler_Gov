"""详情页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin
import hashlib  # 新增

from bs4 import BeautifulSoup

from utils import clean_text, extract_date


# 包含了所有可能的文件后缀
ATTACHMENT_SUFFIX_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?)", re.IGNORECASE)


def _extract_title(soup: BeautifulSoup) -> str:
    """优先从 h1 提取标题，再使用常见标题节点兜底。"""
    selectors = [
        "h1",
        ".article-title",
        ".content-title",
        ".tit",
        "title"
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            title = clean_text(node.get_text(" ", strip=True))
            title = re.sub(r"_.*?国家卫生健康委员会.*$", "", title).strip()
            if title:
                return title
    return ""


def _extract_source(page_text: str) -> str:
    """从页面文本中提取来源部门。"""
    match = re.search(r"来源\s*[:：]\s*([^\s发布时间发布日期]{2,50})", page_text)
    if match:
        return clean_text(match.group(1))

    match = re.search(r"来源\s*[:：]\s*(.{2,50}?)(?:\s+|发布时间|发布日期|$)", page_text)
    return clean_text(match.group(1)) if match else ""


def _find_body_node(soup: BeautifulSoup):
    """寻找正文区域，尽量避开导航和页脚。"""
    selectors = [
        "div.TRS_Editor",
        "div.content",
        "div.article",
        "div#xw_box",
        "div#zoom",
        "div#UCAP-CONTENT",
        "div.pages_content"
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 20:
            return node

    candidates = []
    for node in soup.find_all("div"):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 80:
            candidates.append((len(text), node))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return soup.body or soup


def _extract_attachments(html: str, soup: BeautifulSoup, detail_url: str) -> list[dict]:
    """终极附件提取：底层 HTML 抢救 + 全局扫描 + 文本嗅探。"""
    attachments = []
    seen_urls = set()

    # 1. 原始 HTML 底层抢救逻辑 (对付藏在 script 里的附件)
    raw_a_tags = re.finditer(r'<a\s+[^>]*href=[\'"]([^\'"]+?)[\'"][^>]*>(.*?)</a>', html, re.IGNORECASE)
    for match in raw_a_tags:
        href = match.group(1).strip()
        inner_text = clean_text(re.sub(r'<[^>]+>', '', match.group(2))) 
        
        if not href or href.startswith("javascript:") or href == "#":
            continue
            
        full_url = urljoin(detail_url, href)
        if ATTACHMENT_SUFFIX_RE.search(full_url) and full_url not in seen_urls:
            file_name = inner_text or href.split("/")[-1]
            file_type = file_name.split(".")[-1].lower() if "." in file_name else "unknown"
            attachments.append({
                "name": file_name,
                "url": full_url,
                "file_type": file_type,
                "local_path": "",
                "download_status": "pending"
            })
            seen_urls.add(full_url)

    # 2. 全局扫描与文本兜底嗅探
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        
        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)
        text = clean_text(a_tag.get_text(" ", strip=True))
        title_attr = clean_text(a_tag.get("title", ""))
        
        lower_text = text.lower()
        lower_title = title_attr.lower()

        # 双重校验：后缀命中，或者文字明示了格式
        match = ATTACHMENT_SUFFIX_RE.search(full_url)
        has_doc_text = any(ext in lower_text or ext in lower_title for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.txt', '.zip', '.rar', '.7z'])

        if (match or has_doc_text) and full_url not in seen_urls:
            file_name = title_attr or text or href.split("/")[-1]
            
            if match:
                file_type = match.group(1).lower()
            elif "." in file_name:
                file_type = file_name.split(".")[-1].lower()
            else:
                file_type = "unknown"

            attachments.append({
                "name": file_name,
                "url": full_url,
                "file_type": file_type,
                "local_path": "",               
                "download_status": "pending"    
            })
            seen_urls.add(full_url)

    return attachments


def parse_detail_page(html: str, detail_url: str) -> dict:
    """解析详情页标题、日期、来源、正文和附件。"""
    # 提取附件必须在 decompose(清理) 之前执行！因为我们会用到生肉 html 和未被清理的 soup
    soup_for_extract = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(html, soup_for_extract, detail_url)

    # 正式处理正文：清理无用标签
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

    publish_date = ""
    date_match = re.search(r"(?:发布时间|发布日期)\s*[:：]?\s*((?:20\d{2}|19\d{2})[-./年]\d{1,2}[-./月]\d{1,2}日?)", page_text)
    if date_match:
        publish_date = extract_date(date_match.group(1))
    if not publish_date:
        publish_date = extract_date(page_text)

    source_department = _extract_source(page_text)

    return {
        "title": _extract_title(soup),
        "publish_date": publish_date,
        "source_department": source_department,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images  
    }