"""国家疾病预防控制局 - 列表页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id

DETAIL_SUFFIX_RE = re.compile(r"\.(shtml|html|htm)(?:$|\?)", re.IGNORECASE)
INVALID_TITLE_KEYWORDS = ("首页", "上一页", "下一页", "末页", "尾页", "更多", "返回")


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """构造疾控局列表页分页 URL。
    
    常见的政府网站如 TRS 系统，翻页通常是：
    第 1 页：list.shtml (或 index.htm)
    第 2 页：list_1.shtml 或 list_2.shtml
    这里我们做通用兼容处理。
    """
    if max_pages <= 1:
        return [first_url]

    page_urls = [first_url]
    # 匹配类似 list.shtml 或 index.shtml
    match = re.search(r"/(list|index)\.(shtml|html|htm)$", first_url, re.IGNORECASE)
    if match:
        prefix = first_url[:match.start() + 1] # 拿到前面的路径，包含最后一个 /
        name = match.group(1) # list 或 index
        ext = match.group(2)  # shtml 或 html
        
        for page_no in range(1, max_pages):
            # 有的网站第二页是 _1，有的是 _2。你可以根据实际情况在这里调整
            # 常见的是基于 0 索引的下发，所以第二页通常是 list_1.shtml
            page_urls.append(f"{prefix}{name}_{page_no}.{ext}")
            
    return page_urls


def _is_valid_detail_url(url: str, site_domain: str) -> bool:
    if not url:
        return False
    lower_url = url.lower()
    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    parsed = urlparse(url)
    if parsed.netloc and site_domain not in parsed.netloc:
        # 如果链接跳到了非疾控局的外部网站（比如中国政府网），可以选择过滤掉
        # 也可以放行，取决于你的需求。这里我们默认只抓站内。
        # return False 
        pass 
        
    if not DETAIL_SUFFIX_RE.search(parsed.path):
        return False
    
    if re.search(r"/(list|index)(_\d+)?\.(shtml|html|htm)$", parsed.path, re.IGNORECASE):
        return False
    
    return True


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析疾控局栏目列表页。"""
    soup = BeautifulSoup(html, "lxml")
    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))
    records: list[dict] = []
    seen_urls: set[str] = set()

    # 优先寻找包含文章列表的常见容器
    candidate_nodes = soup.select(".list ul li, .newsList ul li, li")
    if not candidate_nodes:
        candidate_nodes = soup.select("a")

    for node in candidate_nodes:
        a_tag = node if getattr(node, "name", "") == "a" else node.find("a", href=True)
        if not a_tag:
            continue

        title = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
        if title in INVALID_TITLE_KEYWORDS or len(title) < 2:
            continue

        href = a_tag.get("href", "").strip()
        detail_url = urljoin(base_url, href)

        if not _is_valid_detail_url(detail_url, site_domain) or detail_url in seen_urls:
            continue

        node_text = clean_text(node.get_text(" ", strip=True))
        # 很多时候日期会在 a 标签外面的 span 里，所以提整个 node 的文本
        publish_date = extract_date(node_text)

        item = build_empty_document(site_config, channel)
        item["doc_id"] = generate_doc_id(site_domain, channel.get("channel_name", ""), len(records) + 1, publish_date)
        item["title"] = title
        item["url"] = detail_url
        item["dates"]["publish_date"] = publish_date
        item["crawl"]["crawl_status"] = "list_parsed"
        item["raw"]["raw_title"] = title
        item["raw"]["raw_date"] = publish_date

        records.append(item)
        seen_urls.add(detail_url)

    return records