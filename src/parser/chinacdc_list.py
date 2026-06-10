"""中国疾病预防控制中心 - 列表页解析模块 (支持直链文件预判)。"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id

# 增加了 pdf 和 doc 等常见文档后缀
DETAIL_SUFFIX_RE = re.compile(r"\.(shtml|html|htm|pdf|doc|docx)(?:$|\?)", re.IGNORECASE)
INVALID_TITLE_KEYWORDS = ("首页", "上一页", "下一页", "末页", "尾页", "更多", "返回")


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """构造中国疾控中心列表页分页 URL。"""
    if max_pages <= 1:
        return [first_url]

    page_urls = [first_url]
    base_path = first_url
    if first_url.endswith(("index.html", "index.htm", "index.shtml")):
        base_path = first_url.rsplit("/", 1)[0] + "/"
    elif not first_url.endswith("/"):
        base_path = first_url + "/"

    for page_no in range(1, max_pages):
        page_urls.append(f"{base_path}index_{page_no}.html")
        
    return page_urls


def _is_valid_detail_url(url: str, site_domain: str) -> bool:
    """过滤掉无效链接。"""
    if not url:
        return False
    lower_url = url.lower()
    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    parsed = urlparse(url)
    if parsed.netloc and site_domain not in parsed.netloc:
        return False
        
    if not DETAIL_SUFFIX_RE.search(parsed.path):
        return False
        
    # 排除掉翻页的 index_1.html 等链接
    if "index.html" in parsed.path and "_list" not in parsed.path:
        # 注意：如果目录名本身含有 index，这里的过滤需要谨慎
        if re.search(r"index_\d+\.html", parsed.path):
            return False
        
    return True


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """解析栏目列表页，并预判直链文件。"""
    soup = BeautifulSoup(html, "lxml")
    site_domain = site_config.get("site_domain", "")
    base_url = channel.get("channel_url", site_config.get("site_url", ""))
    records: list[dict] = []
    seen_urls: set[str] = set()

    candidate_nodes = soup.select("ul.xw_list > li")
    if not candidate_nodes:
        candidate_nodes = soup.select(".list ul li, .newsList ul li, li")

    for node in candidate_nodes:
        a_tag = node.find("a", href=True)
        if not a_tag:
            continue

        title = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
        if title in INVALID_TITLE_KEYWORDS or len(title) < 4:
            continue

        href = a_tag.get("href", "").strip()
        detail_url = urljoin(base_url, href)

        if not _is_valid_detail_url(detail_url, site_domain) or detail_url in seen_urls:
            continue

        node_text = clean_text(node.get_text(" ", strip=True))
        publish_date = extract_date(node_text)

        item = build_empty_document(site_config, channel)
        item["doc_id"] = generate_doc_id(site_domain, channel.get("channel_name", ""), len(records) + 1, publish_date)
        item["title"] = title
        item["url"] = detail_url
        item["dates"]["publish_date"] = publish_date
        item["raw"]["raw_title"] = title
        item["raw"]["raw_date"] = publish_date

        # --- 核心新增：预判直链文件逻辑 ---
        if detail_url.lower().endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip')):
            item["attachments"].append({
                "name": f"{title}.pdf" if detail_url.lower().endswith('.pdf') else title,
                "url": detail_url,
                "file_type": detail_url.split('.')[-1].lower()
            })
            item["crawl"]["crawl_status"] = "direct_file"  # 标记这是一个直链文件，main.py 会根据它跳过 HTML 解析
        else:
            item["crawl"]["crawl_status"] = "list_parsed"
        # -------------------------------

        records.append(item)
        seen_urls.add(detail_url)

    return records