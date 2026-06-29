"""食品安全国家标准数据检索平台 - 详情页解析模块。

适用详情页示例：
    https://sppt.cfsa.net.cn:8086/db?type=2&guid=...

保存路径：
    src/parser/cfsa_detail.py
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from utils import clean_text, extract_date


UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)

ATTACHMENT_SUFFIX_RE = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?)",
    re.IGNORECASE,
)

# 如果实际下载失败，通常只需要把这里改成浏览器 Network 中 load(guid) 对应的真实接口。
# 解析器会优先从页面 script 中提取真实接口；只有提取不到时才使用这个兜底模板。
FALLBACK_DOWNLOAD_URL_TEMPLATE = "/db/download?guid={guid}"


def _site_origin(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _extract_title(soup: BeautifulSoup, metadata: dict[str, str]) -> str:
    """优先用详情表里的标准名称，其次用页面标题节点兜底。"""
    for key in ("标准名称", "名称", "标题", "文件名称"):
        value = clean_text(metadata.get(key, ""))
        if value:
            return value

    selectors = [
        ".title",
        "h1",
        ".article-title",
        ".content-title",
        ".tit",
        "title",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        title = clean_text(node.get_text(" ", strip=True))
        title = re.sub(r"食品安全国家标准数据检索平台", "", title).strip(" -_")
        if title:
            return title
    return ""


def _extract_metadata(soup: BeautifulSoup) -> dict[str, str]:
    """解析详情页中的标准信息表/Element UI descriptions。"""
    metadata: dict[str, str] = {}

    # 普通 table：支持 2 列或 4 列键值结构。
    for tr in soup.select("table tr"):
        cells = [clean_text(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 4:
            for i in range(0, len(cells) - 1, 2):
                key = cells[i].rstrip(":：")
                value = cells[i + 1]
                if key and value:
                    metadata[key] = value
        elif len(cells) >= 2:
            key = cells[0].rstrip(":：")
            value = cells[1]
            if key and value:
                metadata[key] = value

    # Element UI descriptions：label/content 成对。
    for item in soup.select(".el-descriptions-item, .el-descriptions__cell"):
        label = item.select_one(".el-descriptions-item__label, .el-descriptions__label")
        content = item.select_one(".el-descriptions-item__content, .el-descriptions__content")
        if label and content:
            key = clean_text(label.get_text(" ", strip=True)).rstrip(":：")
            value = clean_text(content.get_text(" ", strip=True))
            if key and value:
                metadata[key] = value

    return metadata


def _find_body_node(soup: BeautifulSoup):
    """寻找详情主体区域。"""
    selectors = [
        "#app .content",
        "div.content",
        "div.code",
        ".el-descriptions",
        ".container",
        "body",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 20:
            return node
    return soup.body or soup


def _file_type_from_name_or_url(name: str, url: str = "") -> str:
    for value in (name, url):
        match = ATTACHMENT_SUFFIX_RE.search(value or "")
        if match:
            return match.group(1).lower()
    if "." in name:
        suffix = name.rsplit(".", 1)[-1].lower()
        if 1 <= len(suffix) <= 5:
            return suffix
    return "unknown"


def _extract_download_url_from_script(html: str, detail_url: str, file_guid: str) -> str:
    """从页面 JS 中尽量还原 load(guid) 的真实下载接口。"""
    file_guid = file_guid.upper()

    # 1. 如果 HTML 里已经出现完整或相对下载地址，直接使用。
    direct_patterns = [
        rf"""["']([^"']*{re.escape(file_guid)}[^"']*)["']""",
        rf"""href\s*=\s*["']([^"']*{re.escape(file_guid)}[^"']*)["']""",
    ]
    for pattern in direct_patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            candidate = match.group(1).replace("&amp;", "&").strip()
            if candidate and not candidate.lower().startswith("javascript:"):
                return urljoin(detail_url, candidate)

    # 2. 尝试解析 function load(x) { location.href = "/xxx?guid=" + x }
    func_match = re.search(
        r"function\s+load\s*\((?P<arg>[^)]*)\)\s*\{(?P<body>.*?)\}",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    script_bodies = [func_match.group("body")] if func_match else []
    script_bodies.extend(script.get_text("\n", strip=False) for script in BeautifulSoup(html, "lxml").find_all("script"))

    for body in script_bodies:
        # "/xxx?guid=" + fileGuid
        match = re.search(
            r"""["'](?P<prefix>[^"']*(?:download|file|attach|load|get)[^"']*(?:guid|id|fileId|fileGuid)?[^"']*[=/&?])["']\s*\+""",
            body,
            re.IGNORECASE,
        )
        if match:
            return urljoin(detail_url, match.group("prefix") + quote(file_guid))

        # url: "/xxx/download", data: { guid: fileGuid } 这类情况无法完整恢复，但可以提取接口路径。
        match = re.search(
            r"""url\s*:\s*["'](?P<url>[^"']*(?:download|file|attach|load|get)[^"']*)["']""",
            body,
            re.IGNORECASE,
        )
        if match:
            url = urljoin(detail_url, match.group("url"))
            joiner = "&" if "?" in url else "?"
            return f"{url}{joiner}guid={quote(file_guid)}"

    # 3. 兜底模板：大概率需要按 Network 里的真实接口微调。
    return urljoin(_site_origin(detail_url), FALLBACK_DOWNLOAD_URL_TEMPLATE.format(guid=quote(file_guid)))


def _extract_attachment_name(file_node, a_tag=None, fallback_guid: str = "") -> str:
    """从附件节点提取文件名，避开按钮文字。"""
    selectors = [
        ".fileItemLeft span",
        ".fileItemLeft",
        ".file-name",
        ".filename",
        ".name",
    ]
    for selector in selectors:
        node = file_node.select_one(selector) if file_node else None
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            text = re.sub(r"\s*(预览|下载)\s*$", "", text).strip()
            if text:
                return text

    if a_tag:
        text = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True))
        text = re.sub(r"\s*(预览|下载)\s*$", "", text).strip()
        if text and text not in {"预览", "下载"}:
            return text

    if file_node:
        text = clean_text(file_node.get_text(" ", strip=True))
        text = re.sub(r"\s*(预览|下载)\s*", " ", text)
        text = clean_text(text)
        if text:
            return text

    return fallback_guid or "未命名附件"


def _extract_attachments(html: str, soup: BeautifulSoup, detail_url: str) -> list[dict]:
    """提取详情页附件，兼容 href 直链和 onclick=load('GUID')。"""
    attachments: list[dict] = []
    seen: set[str] = set()

    def add_attachment(name: str, url: str, guid: str = "") -> None:
        if not url or url in seen:
            return
        file_type = _file_type_from_name_or_url(name, url)
        attachments.append({
            "name": name or guid or url.split("/")[-1] or "未命名附件",
            "url": url,
            "file_type": file_type,
            "local_path": "",
            "download_status": "pending",
        })
        seen.add(url)

    # 1. 优先解析该站附件区域。
    file_nodes = soup.select(".file .fileItem, .fileItem, .attachments li, .attachment li")
    for file_node in file_nodes:
        node_html = str(file_node)

        # href 直链附件。
        for a_tag in file_node.find_all("a", href=True):
            href = (a_tag.get("href") or "").strip()
            if href and not href.lower().startswith(("javascript:", "#", "mailto:")):
                full_url = urljoin(detail_url, href)
                if ATTACHMENT_SUFFIX_RE.search(full_url):
                    name = _extract_attachment_name(file_node, a_tag)
                    add_attachment(name, full_url)

        # JS 下载：onclick="load('UUID')"
        guid_match = UUID_RE.search(node_html)
        if guid_match:
            guid = guid_match.group(0).upper()
            url = _extract_download_url_from_script(html, detail_url, guid)
            name = _extract_attachment_name(file_node, fallback_guid=guid)
            add_attachment(name, url, guid)

    # 2. 全局 href 扫描兜底。
    for a_tag in soup.find_all("a", href=True):
        href = (a_tag.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        full_url = urljoin(detail_url, href)
        if not ATTACHMENT_SUFFIX_RE.search(full_url):
            continue
        name = clean_text(a_tag.get("title", "")) or clean_text(a_tag.get_text(" ", strip=True)) or full_url.split("/")[-1]
        add_attachment(name, full_url)

    # 3. 全局 onclick=load('UUID') 扫描兜底。
    for tag in soup.find_all(attrs={"onclick": True}):
        onclick = tag.get("onclick") or ""
        if "load" not in onclick.lower():
            continue
        guid_match = UUID_RE.search(onclick)
        if not guid_match:
            continue
        guid = guid_match.group(0).upper()
        url = _extract_download_url_from_script(html, detail_url, guid)
        name = _extract_attachment_name(tag.parent or tag, tag, guid)
        add_attachment(name, url, guid)

    return attachments


def _extract_images(body_node, detail_url: str) -> list[dict]:
    """提取正文图片，并把 body_html 中的 src 改成本地相对路径。"""
    images: list[dict] = []
    if not body_node:
        return images

    for img in body_node.find_all("img"):
        src = img.get("src")
        if not src:
            continue

        full_url = urljoin(detail_url, src)
        ext = src.split(".")[-1].split("?")[0].lower()
        if not ext or len(ext) > 5:
            ext = "jpg"

        img_name = f"img_{hashlib.md5(full_url.encode('utf-8')).hexdigest()[:12]}.{ext}"
        images.append({
            "url": full_url,
            "file_name": img_name,
            "local_path": "",
            "download_status": "pending",
        })
        img["src"] = f"images/{img_name}"

    return images


def _extract_publish_date(metadata: dict[str, str], page_text: str) -> str:
    """提取发布日期。"""
    for key in ("发布日期", "发布时间", "发布日"):
        value = metadata.get(key, "")
        if value:
            date_value = extract_date(value)
            if date_value:
                return date_value

    match = re.search(
        r"(?:发布日期|发布时间)\s*[:：]?\s*((?:19|20)\d{2}[-./年]\d{1,2}[-./月]\d{1,2}日?)",
        page_text,
    )
    if match:
        return extract_date(match.group(1))

    return extract_date(page_text)


def parse_detail_page(html: str, detail_url: str) -> dict:
    """解析详情页标题、发布日期、正文、附件和图片。"""
    soup_for_extract = BeautifulSoup(html, "lxml")
    attachments = _extract_attachments(html, soup_for_extract, detail_url)

    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    metadata = _extract_metadata(soup)
    body_node = _find_body_node(soup)
    images = _extract_images(body_node, detail_url)

    page_text = clean_text(soup.get_text(" ", strip=True))
    title = _extract_title(soup, metadata)
    publish_date = _extract_publish_date(metadata, page_text)

    metadata_text = "；".join(f"{key}：{value}" for key, value in metadata.items() if value)
    body_text = clean_text(body_node.get_text(" ", strip=True)) if body_node else ""
    if metadata_text and metadata_text not in body_text:
        body_text = clean_text(f"{metadata_text} {body_text}")

    return {
        "title": title,
        "publish_date": publish_date,
        "source_department": "食品安全国家标准数据检索平台",
        "body_text": body_text,
        "body_html": str(body_node) if body_node else "",
        "attachments": attachments,
        "images": images,
    }
