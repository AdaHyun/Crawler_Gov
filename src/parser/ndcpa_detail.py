"""国家疾病预防控制局 - 详情页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils import clean_text, extract_date

import hashlib

# ATTACHMENT_SUFFIX_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|zip)(?:$|\?)", re.IGNORECASE)
ATTACHMENT_SUFFIX_RE = re.compile(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?)", re.IGNORECASE)


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
    # ================= 新增：原始 HTML 底层抢救逻辑 =================
    # 在 BeautifulSoup 清理之前，暴力抓取藏在 <script> document.write 里的附件
    attachments = []
    seen_urls = set()
    
    raw_a_tags = re.finditer(r'<a\s+[^>]*href=[\'"]([^\'"]+?)[\'"][^>]*>(.*?)</a>', html, re.IGNORECASE)
    for match in raw_a_tags:
        href = match.group(1).strip()
        # 移除可能嵌套的 HTML 标签，提取纯文本
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
    # ================================================================

    soup = BeautifulSoup(html, "lxml")

    # 去掉脚本和样式 (此操作会销毁 script 标签，但上面我们已经把里面的附件救出来了)
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

    # ================= 附件提取逻辑 (增强版) =================
    # 扩大搜索范围：扫描全网页 (soup) 而不仅仅是正文容器 (body_node)
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        
        if not href or href.startswith("javascript:") or href == "#":
            continue

        full_url = urljoin(detail_url, href)
        
        # 获取标签的文本和 title 属性
        text = clean_text(a_tag.get_text(" ", strip=True))
        title_attr = clean_text(a_tag.get("title", ""))
        
        lower_text = text.lower()
        lower_title = title_attr.lower()

        # 匹配逻辑：URL 后缀命中，或者链接的文字内容明示了它是一个文档
        match = ATTACHMENT_SUFFIX_RE.search(full_url)
        # has_doc_text = any(ext in lower_text or ext in lower_title for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar'])
        has_doc_text = any(ext in lower_text or ext in lower_title for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv', '.txt', '.zip', '.rar', '.7z'])

        if (match or has_doc_text) and full_url not in seen_urls:
            # 优先使用 title 属性命名
            file_name = title_attr or text or href.split("/")[-1]
            
            # 推断文件后缀类型
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
    # ==================================================================

    return {
        "title": _extract_title(soup),
        "publish_date": publish_date,
        "source_department": source_department,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
        "images": images  
    }