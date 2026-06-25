"""国家心理健康和精神卫生防治中心 - 列表页解析模块。"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from utils import build_empty_document, clean_text, extract_date, generate_doc_id


FILE_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".csv", ".txt", ".zip", ".rar", ".7z"
)

INVALID_TITLE_KEYWORDS = (
    "首页", "上一页", "下一页", "末页", "尾页", "更多", "返回",
    "内部登录", "外部登录", "联系我们", "手机版", "查看更多", "查看更多>>"
)


def build_page_urls(first_url: str, max_pages: int = 1) -> list[str]:
    """
    构造国家心理健康和精神卫生防治中心分页 URL。

    新闻资讯栏目：
        第 1 页：https://ncmhc.org.cn/news
        第 2 页：https://ncmhc.org.cn/channel_index/news/page/2
        第 3 页：https://ncmhc.org.cn/channel_index/news/page/3

    政策法规总页：
        https://ncmhc.org.cn/channel_index/news_zc

    政策法规总页本身是分区聚合页，不按 /page/2 翻页。
    """
    if max_pages <= 1:
        return [first_url]

    parsed = urlparse(first_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    # 政策法规总页是聚合页，里面按“第一部分/第二部分/第三部分”分区。
    # 不直接构造 page/2，避免请求不存在的分页。
    if path.endswith("/channel_index/news_zc"):
        return [first_url]

    # 新闻资讯首页 /news
    if path == "/news":
        page_urls = [first_url]
        for page_no in range(2, max_pages + 1):
            page_urls.append(f"{root}/channel_index/news/page/{page_no}")
        return page_urls

    # 新闻资讯列表 /channel_index/news
    if path == "/channel_index/news":
        page_urls = [first_url]
        for page_no in range(2, max_pages + 1):
            page_urls.append(f"{root}/channel_index/news/page/{page_no}")
        return page_urls

    # 其他栏目默认只抓第一页。
    return [first_url]


def _same_domain_or_empty(url: str, site_domain: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc:
        return True
    return site_domain in parsed.netloc


def _is_valid_article_url(url: str, site_domain: str) -> bool:
    """判断是否是该站文章详情链接。"""
    if not url:
        return False

    lower_url = url.lower()

    if lower_url.startswith(("javascript:", "mailto:", "#")):
        return False

    parsed = urlparse(url)

    if not _same_domain_or_empty(url, site_domain):
        return False

    # 该站文章详情页主要是这种格式：
    # /channel/newsinfo/8100
    if "/channel/newsinfo/" in parsed.path:
        return True

    # 预留：如果列表页直接出现文件链接，也允许进入。
    if lower_url.split("?")[0].endswith(FILE_SUFFIXES):
        return True

    return False


def _extract_source(text: str) -> str:
    """从列表项文本里提取来源。"""
    if not text:
        return ""

    match = re.search(r"(?:来源|信息来源)\s*[:：]\s*([^\s|｜]+)", text)
    if match:
        return clean_text(match.group(1))

    return ""


def _extract_url_id(url: str, fallback: int) -> int:
    """
    用详情页 URL 里的数字 ID 生成稳定 doc_id。

    例如：
        /channel/newsinfo/8100 -> 8100
    """
    match = re.search(r"/newsinfo/(\d+)", url)
    if match:
        return int(match.group(1))
    return fallback


def _extract_title(a_tag) -> str:
    """从 a 标签中提取标题。"""
    title = (
        clean_text(a_tag.get("title", ""))
        or clean_text(a_tag.get_text(" ", strip=True))
    )

    if not title:
        return ""

    title = re.sub(r"\s+", " ", title).strip()

    if title in INVALID_TITLE_KEYWORDS:
        return ""

    # 排除“查看更多>>”这类链接
    if "查看更多" in title:
        return ""

    return title


def _build_item(
    a_tag,
    node,
    section_name: str,
    channel: dict,
    site_config: dict,
    base_url: str,
    index: int,
) -> dict | None:
    """
    构造统一 schema 的 item。

    section_name:
        普通新闻资讯栏目为空；
        政策法规栏目为“第一部分 法律 / 第二部分 国务院文件”等。
    """
    site_domain = site_config.get("site_domain", "")

    href = a_tag.get("href", "").strip()
    detail_url = urljoin(base_url, href)

    if not _is_valid_article_url(detail_url, site_domain):
        return None

    title = _extract_title(a_tag)

    if not title or len(title) < 4:
        return None

    node_text = clean_text(node.get_text(" ", strip=True)) if node else title

    publish_date = extract_date(node_text)
    source_department = _extract_source(node_text)

    item = build_empty_document(site_config, channel)

    url_index = _extract_url_id(detail_url, index)

    item["doc_id"] = generate_doc_id(
        site_domain,
        channel.get("channel_name", ""),
        url_index,
        publish_date
    )

    item["title"] = title
    item["url"] = detail_url

    item["dates"]["publish_date"] = publish_date

    if source_department:
        item["organization"]["source_department"] = source_department

    item["raw"]["raw_title"] = title
    item["raw"]["raw_date"] = publish_date
    item["raw"]["raw_source"] = source_department

    # 关键逻辑：
    # 只有政策法规这种分区页面才会写入 section_name。
    # main.py 后面会读取 crawl.asset_subdir_parts，决定图片/附件是否多加一级目录。
    if section_name:
        item["raw"]["section_name"] = section_name
        item["crawl"]["asset_subdir_parts"] = [section_name]

        if section_name not in item["classification"]["topic_tags"]:
            item["classification"]["topic_tags"].append(section_name)

    clean_url = detail_url.lower().split("?")[0]

    if clean_url.endswith(FILE_SUFFIXES):
        file_type = clean_url.rsplit(".", 1)[-1]
        item["attachments"].append({
            "name": f"{title}.{file_type}" if not title.lower().endswith(f".{file_type}") else title,
            "url": detail_url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending"
        })
        item["crawl"]["crawl_status"] = "direct_file"
    else:
        item["crawl"]["crawl_status"] = "list_parsed"

    return item


def parse_list_page(html: str, channel: dict, site_config: dict) -> list[dict]:
    """
    解析列表页。

    支持两类页面：

    1. 新闻资讯栏目：
        https://ncmhc.org.cn/news
        https://ncmhc.org.cn/channel_index/news/page/2

        页面里直接有：
        <a href="/channel/newsinfo/xxxx">标题</a>

    2. 政策法规栏目：
        https://ncmhc.org.cn/channel_index/news_zc

        页面按模块分区：
        <div class="home_yuanzhu_box">
            <ul class="channel_title">
                <li>第一部分 法律</li>
            </ul>
            ...
            <a href="/channel/newsinfo/6251">文章标题</a>
        </div>
    """
    soup = BeautifulSoup(html, "lxml")

    base_url = channel.get("channel_url", site_config.get("site_url", ""))

    records: list[dict] = []
    seen_urls: set[str] = set()

    # ============================================================
    # 1. 优先解析“政策法规”这种分区页面
    # ============================================================
    section_boxes = soup.select(".home_yuanzhu_box")

    for box in section_boxes:
        section_node = box.select_one("ul.channel_title li")
        section_name = clean_text(section_node.get_text(" ", strip=True)) if section_node else ""

        # 只抓文章详情链接，不抓“查看更多”
        article_links = box.select("a[href*='/channel/newsinfo/']")

        for a_tag in article_links:
            node = (
                a_tag.find_parent("li")
                or a_tag.find_parent("div")
                or a_tag.parent
            )

            item = _build_item(
                a_tag=a_tag,
                node=node,
                section_name=section_name,
                channel=channel,
                site_config=site_config,
                base_url=base_url,
                index=len(records) + 1,
            )

            if not item:
                continue

            if item["url"] in seen_urls:
                continue

            records.append(item)
            seen_urls.add(item["url"])

    # 如果已经按分区解析到了文章，直接返回。
    # 这样政策法规栏目就会保留“第一部分/第二部分/第三部分”路径信息。
    if records:
        return records

    # ============================================================
    # 2. 普通新闻资讯页面
    # ============================================================
    article_links = soup.select("a[href*='/channel/newsinfo/']")

    for a_tag in article_links:
        node = (
            a_tag.find_parent("li")
            or a_tag.find_parent("div")
            or a_tag.parent
        )

        item = _build_item(
            a_tag=a_tag,
            node=node,
            section_name="",
            channel=channel,
            site_config=site_config,
            base_url=base_url,
            index=len(records) + 1,
        )

        if not item:
            continue

        if item["url"] in seen_urls:
            continue

        records.append(item)
        seen_urls.add(item["url"])

    return records