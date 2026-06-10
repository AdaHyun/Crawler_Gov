"""国家疾病预防控制局 - 详情页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils import clean_text, extract_date

import hashlib

ATTACHMENT_SUFFIX_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|zip)(?:$|\?)", re.IGNORECASE)


def _extract_title(soup: BeautifulSoup) -> str:
    # 疾控局文章标题常见的 class
    selectors = [
        "h1", 
        ".title", 
        ".article-title", 
        ".xw_title"
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            title = clean_text(node.get_text(" ", strip=True))
            if title:
                return title
    return ""


def _find_body_node(soup: BeautifulSoup):
    """寻找疾控局正文区域。"""
    selectors = [
        "div.TRS_Editor",    # TRS 系统默认正文 class
        "div.article_content",
        "div.content",
        "div#Zoom",          # 有些网站用这个控制字体缩放
        "div.Custom_UnionStyle"
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 20:
            return node

    # 兜底逻辑
    return soup.body or soup


import hashlib  # 请确保文件最上方有这个导入

def parse_detail_page(html: str, detail_url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # 去掉脚本和样式
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    body_node = _find_body_node(soup)

    # ================= 图片提取与本地化换链逻辑 =================
    images = []
    if body_node:
        for img in body_node.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            
            full_url = urljoin(detail_url, src)
            
            # 提取图片后缀名 (比如 .jpg, .png)
            ext = src.split('.')[-1].split('?')[0]
            if len(ext) > 5 or not ext: 
                ext = "jpg" # 兜底后缀
                
            # 用网址的 MD5 哈希命名图片，防止同名覆盖
            img_name = f"img_{hashlib.md5(full_url.encode()).hexdigest()[:12]}.{ext}"
            
            # 记录到待下载列表
            images.append({
                "url": full_url,
                "file_name": img_name,
                "local_path": "",
                "download_status": "pending"
            })
            
            # 【核心】把 HTML 源码里原本的网络图片地址，替换成相对本地地址
            img["src"] = f"images/{img_name}" 
    # ==================================================================

    # 提取正文文本和处理过图片链接的 HTML
    page_text = clean_text(soup.get_text(" ", strip=True))
    body_text = clean_text(body_node.get_text(" ", strip=True)) if body_node else ""
    body_html = str(body_node) if body_node else ""

    # 提取日期
    publish_date = extract_date(page_text)

    # 提取来源
    source_department = "国家疾病预防控制局" # 默认值
    source_match = re.search(r"来源\s*[:：]\s*([^\s]{2,20})", page_text)
    if source_match:
        source_department = clean_text(source_match.group(1))

    # 提取附件
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
                "local_path": "",               
                "download_status": "pending"    
            })
            seen_urls.add(full_url)

    return {
        "title": _extract_title(soup),
        "publish_date": publish_date,
        "source_department": source_department,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images  
    }