"""WHO Publications crawler/parser demo."""

from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qsl

from bs4 import BeautifulSoup

from utils import clean_text
from parser.who.schema import build_who_base_record
from parser.who.utils import (
    download_attachment,
    enrich_attachment,
    generate_doc_id,
    hash_text,
    hash_url,
    is_final_file_url,
    is_iris_landing_url,
    load_doc_id_registry,
    save_doc_id_registry,
    save_raw_html,
)


FetchHtml = Callable[..., tuple[str, int]]

ATTACHMENT_SUFFIX_RE = re.compile(
    r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|csv|txt|zip|rar|7z)(?:$|\?)",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{1,2}-\d{1,2})"
)
SKIP_LINK_TEXT = {"read more", "download", "all", "publications"}
WHO_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}


def parse_who_publications(
    site_config: dict[str, Any],
    fetch_html: FetchHtml,
    logger=None,
) -> list[dict[str, Any]]:
    """Fetch and parse a small WHO Publications demo run."""
    max_pages = int(site_config.get("max_pages", 1))
    timeout = int(site_config.get("request", {}).get("timeout", 20))
    list_urls = _build_list_urls(site_config.get("channel_url", ""), max_pages)
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    registry = load_doc_id_registry()

    for page_index, list_url in enumerate(list_urls, start=1):
        try:
            list_html, list_status = fetch_html(
                list_url,
                headers=WHO_HEADERS,
                referer=site_config.get("site_url", "https://www.who.int"),
                timeout=timeout,
            )
            save_raw_html(list_html, f"WHO-PUB-list-{page_index:02d}", "publications")
            list_items = _parse_publication_list(list_html, list_url)
            if logger:
                logger.info("[WHO Publications] page %s parsed %s items", page_index, len(list_items))
        except Exception as exc:
            if logger:
                logger.exception("[WHO Publications] list page failed: %s", list_url)
            records.append(_build_failed_record(site_config, list_url, exc, "list_failed", registry, page_index))
            continue

        if not list_items:
            break

        for item_index, item in enumerate(list_items, start=1):
            detail_url = item["url"]
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            record = build_who_base_record(site_config)
            record["title"] = item.get("title", "")
            record["url"] = detail_url
            record["dates"]["publish_date"] = item.get("publish_date", "")
            record["content"]["summary"] = item.get("summary", "")
            record["classification"]["document_type"] = "publication"
            record["who_metadata"]["publication_type"] = "publication"
            record["raw"]["raw_title"] = item.get("title", "")
            record["raw"]["raw_date"] = item.get("publish_date", "")
            record["crawl"]["http_status"] = list_status
            record["crawl"]["crawl_status"] = "list_parsed"
            record["crawl"]["url_hash"] = hash_url(detail_url)

            try:
                detail_html, detail_status = fetch_html(
                    detail_url,
                    headers=WHO_HEADERS,
                    referer=list_url,
                    timeout=timeout,
                )
                detail = _parse_publication_detail(detail_html, detail_url)
                _merge_detail(record, detail)
                doc_id, url_hash = generate_doc_id(
                    detail_url,
                    record["dates"].get("publish_date", ""),
                    site_config,
                    batch_no=page_index,
                    registry=registry,
                    record=record,
                )
                record["doc_id"] = doc_id
                record["crawl"]["url_hash"] = url_hash
                raw_detail_path = save_raw_html(detail_html, record["doc_id"], "publications")
                _process_attachments(record, site_config)
                record["crawl"]["http_status"] = detail_status
                record["crawl"]["raw_html_path"] = raw_detail_path
                record["crawl"]["text_hash"] = hash_text(record["content"]["body_text"])
                record["crawl"]["crawl_status"] = "success"
                record["crawl"]["error_message"] = ""
                save_doc_id_registry(registry)
            except Exception as exc:
                if not record.get("doc_id"):
                    doc_id, url_hash = generate_doc_id(
                        detail_url,
                        record["dates"].get("publish_date", ""),
                        site_config,
                        batch_no=page_index,
                        registry=registry,
                        record=record,
                    )
                    record["doc_id"] = doc_id
                    record["crawl"]["url_hash"] = url_hash
                    save_doc_id_registry(registry)
                record["crawl"]["crawl_status"] = "detail_failed"
                record["crawl"]["error_message"] = str(exc)
                if logger:
                    logger.error("[WHO Publications] detail failed: %s error=%s", detail_url, exc)

            records.append(record)
            time.sleep(0.8)

    save_doc_id_registry(registry)
    return records


def _build_list_urls(first_url: str, max_pages: int) -> list[str]:
    if not first_url:
        return []
    urls = [first_url]
    for page_no in range(2, max_pages + 1):
        parsed = urlparse(first_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page_no)
        urls.append(urlunparse(parsed._replace(query=urlencode(query))))
    return urls


def _parse_publication_list(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        if "who.int" not in parsed.netloc or "/publications/i/item/" not in parsed.path:
            continue
        if full_url in seen_urls:
            continue

        title = clean_text(a_tag.get_text(" ", strip=True))
        if title.lower() in SKIP_LINK_TEXT or len(title) < 8:
            title = _nearby_title(a_tag)
        if not title or title.lower() in SKIP_LINK_TEXT:
            continue

        parent = _find_card_node(a_tag)
        parent_text = clean_text(parent.get_text(" ", strip=True)) if parent else ""
        summary = _extract_summary(parent, title) if parent else ""

        items.append(
            {
                "title": title,
                "url": full_url,
                "publish_date": _extract_date(parent_text),
                "summary": summary,
            }
        )
        seen_urls.add(full_url)

    return items


def _parse_publication_detail(html: str, detail_url: str) -> dict[str, Any]:
    soup_for_links = BeautifulSoup(html, "lxml")
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    main_node = _find_main_node(soup)
    title = _extract_title(soup)
    main_text = clean_text(main_node.get_text(" ", strip=True)) if main_node else clean_text(soup.get_text(" ", strip=True))
    publish_date = _extract_date(main_text)
    publication_type = _extract_publication_type(main_text)
    overview_node = _extract_overview_node(soup)
    summary = clean_text(overview_node.get_text(" ", strip=True)) if overview_node else ""
    body_html = str(main_node) if main_node else ""

    return {
        "title": title,
        "publish_date": publish_date,
        "publication_type": publication_type,
        "summary": summary,
        "body_text": main_text,
        "body_html": body_html,
        "attachments": _extract_attachments(soup_for_links, detail_url),
        "images": _extract_images(main_node or soup_for_links, detail_url),
        "raw_source": _extract_who_team(main_text),
    }


def _merge_detail(record: dict[str, Any], detail: dict[str, Any]) -> None:
    record["title"] = detail.get("title") or record.get("title", "")
    record["dates"]["publish_date"] = detail.get("publish_date") or record["dates"].get("publish_date", "")
    record["content"]["body_text"] = detail.get("body_text", "")
    record["content"]["body_html"] = detail.get("body_html", "")
    record["content"]["summary"] = detail.get("summary") or record["content"].get("summary", "")
    record["content"]["abstract"] = record["content"]["summary"]
    record["attachments"] = detail.get("attachments", [])
    record["images"] = detail.get("images", [])
    record["classification"]["document_type"] = detail.get("publication_type") or "publication"
    record["who_metadata"]["publication_type"] = detail.get("publication_type", "")
    record["raw"]["raw_title"] = record["title"]
    record["raw"]["raw_date"] = record["dates"]["publish_date"]
    record["raw"]["raw_source"] = detail.get("raw_source", "")


def _process_attachments(record: dict[str, Any], site_config: dict[str, Any]) -> None:
    processed = []
    for index, attachment in enumerate(record.get("attachments", []), start=1):
        enriched = enrich_attachment(attachment, site_config)
        enriched = download_attachment(record, enriched, site_config, attachment_index=index)
        processed.append(enriched)
    record["attachments"] = processed


def _build_failed_record(
    site_config: dict[str, Any],
    url: str,
    exc: Exception,
    status: str,
    registry: dict[str, Any],
    batch_no: int = 1,
) -> dict[str, Any]:
    record = build_who_base_record(site_config)
    doc_id, url_hash = generate_doc_id(url, "", site_config, batch_no=batch_no, registry=registry, record=record)
    record["doc_id"] = doc_id
    record["url"] = url
    record["crawl"]["url_hash"] = url_hash
    record["crawl"]["crawl_status"] = status
    record["crawl"]["error_message"] = str(exc)
    return record


def _find_card_node(node):
    for parent in node.parents:
        if not getattr(parent, "name", None):
            continue
        text = clean_text(parent.get_text(" ", strip=True))
        if len(text) > 40 and DATE_RE.search(text):
            return parent
        if parent.name in {"article", "li"}:
            return parent
    return node.parent


def _nearby_title(a_tag) -> str:
    parent = _find_card_node(a_tag)
    if not parent:
        return ""
    for candidate in parent.find_all(["h2", "h3", "h4", "a"]):
        text = clean_text(candidate.get_text(" ", strip=True))
        if text and text.lower() not in SKIP_LINK_TEXT and len(text) > 8:
            return text
    return ""


def _extract_summary(parent, title: str) -> str:
    if not parent:
        return ""
    paragraphs = []
    for node in parent.find_all(["p", "div", "span"]):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 40 and text != title and not DATE_RE.fullmatch(text):
            paragraphs.append(text)
    return paragraphs[0] if paragraphs else ""


def _find_main_node(soup: BeautifulSoup):
    selectors = [
        "main",
        "article",
        ".sf-detail-body-wrapper",
        ".publication-details",
        ".body-content",
        ".content",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node and len(clean_text(node.get_text(" ", strip=True))) > 80:
            return node
    return soup.body or soup


def _extract_title(soup: BeautifulSoup) -> str:
    for selector in ["h1", "meta[property='og:title']", "title"]:
        node = soup.select_one(selector)
        if not node:
            continue
        title = clean_text(node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True))
        title = re.sub(r"\s*-\s*WHO\s*$", "", title).strip()
        if title:
            return title
    return ""


def _extract_date(text: str) -> str:
    match = DATE_RE.search(text or "")
    return match.group(1) if match else ""


def _extract_publication_type(text: str) -> str:
    match = re.search(r"\|\s*([A-Za-z][A-Za-z /-]{2,40}?)(?:\s+Download|\s+Overview|$)", text or "")
    if match:
        return clean_text(match.group(1)).lower()
    lowered = (text or "").lower()
    for word in ("report", "guideline", "manual", "toolkit", "publication"):
        if word in lowered:
            return word
    return "publication"


def _extract_overview_node(soup: BeautifulSoup):
    heading = None
    for node in soup.find_all(re.compile("^h[1-6]$")):
        if clean_text(node.get_text(" ", strip=True)).lower() == "overview":
            heading = node
            break
    if not heading:
        return None
    parts = []
    for sibling in heading.find_next_siblings():
        if getattr(sibling, "name", "") in {"h2", "h3"}:
            break
        parts.append(str(sibling))
    if not parts:
        return None
    return BeautifulSoup("".join(parts), "lxml")


def _extract_who_team(text: str) -> str:
    match = re.search(r"WHO Team\s+(.+?)(?:Editors|Number of pages|Reference numbers|Copyright|$)", text or "")
    return clean_text(match.group(1)) if match else ""


def _extract_attachments(soup: BeautifulSoup, detail_url: str) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "").strip()
        full_url = urljoin(detail_url, href)
        text = clean_text(a_tag.get_text(" ", strip=True))
        match = ATTACHMENT_SUFFIX_RE.search(full_url)
        is_download = "download" in text.lower() or "iris.who.int" in urlparse(full_url).netloc
        if not match and not is_download:
            continue
        if full_url in seen_urls:
            continue
        file_type = match.group(1).lower() if match else "unknown"
        landing_url = full_url if is_iris_landing_url(full_url) else ""
        final_file_url = full_url if is_final_file_url(full_url) else ""
        attachments.append(
            {
                "name": text or full_url.rstrip("/").split("/")[-1],
                "url": final_file_url or landing_url or full_url,
                "landing_url": landing_url,
                "final_file_url": final_file_url,
                "file_type": file_type,
                "mime_type": "",
                "file_size": _extract_file_size(text),
                "local_path": "",
                "download_status": "link_only",
                "file_hash": "",
            }
        )
        seen_urls.add(full_url)
    return attachments


def _extract_images(soup: BeautifulSoup, detail_url: str) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        full_url = urljoin(detail_url, src)
        if full_url in seen_urls:
            continue
        images.append({"url": full_url, "local_path": "", "download_status": "pending"})
        seen_urls.add(full_url)
    return images


def _extract_file_size(text: str) -> str:
    match = re.search(r"\(([^)]*(?:KB|MB|GB)[^)]*)\)", text or "", re.IGNORECASE)
    return match.group(1).strip() if match else ""
