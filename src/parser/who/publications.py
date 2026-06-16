"""WHO Publications crawler/parser demo."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qsl

from bs4 import BeautifulSoup

from utils import PROJECT_ROOT, append_jsonl, clean_text
from parser.who.schema import build_who_base_record
from parser.who.utils import (
    download_attachment,
    enrich_attachment,
    extract_publish_year,
    generate_doc_id,
    hash_text,
    hash_url,
    is_final_file_url,
    is_iris_landing_url,
    load_doc_id_registry,
    normalize_url,
    save_doc_id_registry,
    save_raw_html,
    safe_filename,
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
WHO_PUBLICATIONS_API_URL = (
    "https://www.who.int/api/hubs/publications"
    "?sf_site=15210d59-ad60-47ff-a542-7ed76645f0c7"
    "&sf_provider=OpenAccessProvider"
    "&sf_culture=en"
)
WHO_PUBLICATIONS_PAGE_SIZE = 40
WHO_PUBLICATIONS_OUTPUT_PATH = PROJECT_ROOT / "data" / "output" / "who" / "who_publications.jsonl"
LANGUAGE_CODE_MAP = {
    "english": "en",
    "chinese": "zh",
    "中文": "zh",
    "español": "es",
    "espanol": "es",
    "spanish": "es",
    "français": "fr",
    "francais": "fr",
    "french": "fr",
    "العربية": "ar",
    "arabic": "ar",
    "русский": "ru",
    "russian": "ru",
    "português": "pt",
    "portugues": "pt",
    "portuguese": "pt",
}
DETAIL_LABELS = {
    "who team",
    "editors",
    "number of pages",
    "reference numbers",
    "copyright",
}


def parse_who_publications(
    site_config: dict[str, Any],
    fetch_html: FetchHtml,
    logger=None,
) -> list[dict[str, Any]]:
    """Fetch and parse WHO Publications with checkpoint/resume support."""
    max_pages = int(site_config.get("max_pages", 1))
    timeout = int(site_config.get("request", {}).get("timeout", 20))
    list_urls = _build_list_urls(site_config.get("channel_url", ""), max_pages)
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    registry = load_doc_id_registry()
    existing_url_hashes = _load_existing_url_hashes(registry) if site_config.get("skip_existing", True) else set()
    checkpoint = _load_checkpoint(site_config)
    checkpoint_interval = max(int(site_config.get("checkpoint_interval", 1)), 1)
    start_page = 1
    if site_config.get("resume", True):
        start_page = max(int(checkpoint.get("last_finished_page", 0)), 1)

    if logger:
        logger.info(
            "[WHO Publications] resume=%s checkpoint=%s",
            bool(site_config.get("resume", True)),
            _checkpoint_path(site_config).relative_to(PROJECT_ROOT).as_posix(),
        )
        logger.info("[WHO Publications] resume from page=%s", start_page)

    try:
        for page_index, list_url in enumerate(list_urls, start=1):
            if page_index < start_page:
                continue

            page_saved = 0
            page_skipped = 0
            page_failed = 0
            try:
                list_html, list_status = fetch_html(
                    list_url,
                    headers=WHO_HEADERS,
                    referer=site_config.get("site_url", "https://www.who.int"),
                    timeout=timeout,
                )
                if not site_config.get("resume", True) or page_index >= start_page:
                    save_raw_html(list_html, f"WHO-PUB-list-{page_index:02d}", "publications")
                list_items = _parse_publication_list(list_html, list_url)
                if logger:
                    logger.info("[WHO Publications] page %s parsed %s items", page_index, len(list_items))
            except Exception as exc:
                page_failed += 1
                checkpoint["total_failed_records"] += 1
                if logger:
                    logger.exception("[WHO Publications] list page failed: %s", list_url)
                failed_record = _build_failed_record(site_config, list_url, exc, "list_failed", registry, page_index)
                _append_who_publication_record(failed_record)
                records.append(failed_record)
                checkpoint["last_finished_page"] = page_index
                checkpoint["last_finished_item_index"] = 0
                if page_index % checkpoint_interval == 0:
                    _save_checkpoint(site_config, checkpoint, logger)
                continue

            if not list_items:
                break

            for item_index, item in enumerate(list_items, start=1):
                checkpoint["total_seen_items"] += 1
                detail_url = item["url"]
                item_url_hash = hash_url(detail_url)
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                if site_config.get("skip_existing", True) and item_url_hash in existing_url_hashes:
                    page_skipped += 1
                    checkpoint["total_skipped_existing"] += 1
                    checkpoint["last_finished_item_index"] = item_index
                    continue

                record = build_who_base_record(site_config)
                record["title"] = item.get("title", "")
                record["url"] = detail_url
                record["dates"]["publish_date"] = item.get("publish_date", "")
                record["dates"]["publish_year"] = extract_publish_year(record["dates"]["publish_date"])
                record["content"]["summary"] = item.get("summary", "")
                record["classification"]["document_type"] = "publication"
                record["who_metadata"]["publication_type"] = "publication"
                record["raw"]["raw_title"] = item.get("title", "")
                record["raw"]["raw_date"] = item.get("publish_date", "")
                record["crawl"]["http_status"] = list_status
                record["crawl"]["crawl_status"] = "list_parsed"
                record["crawl"]["url_hash"] = item_url_hash

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
                    _process_language_versions(record, detail, site_config, fetch_html, detail_url, timeout, logger)
                    _process_attachments(record, site_config)
                    record["crawl"]["http_status"] = detail_status
                    record["crawl"]["raw_html_path"] = raw_detail_path
                    record["crawl"]["text_hash"] = hash_text(record["content"]["body_text"])
                    record["crawl"]["crawl_status"] = "success"
                    record["crawl"]["error_message"] = ""
                    save_doc_id_registry(registry)
                    existing_url_hashes.add(url_hash)
                    page_saved += 1
                    checkpoint["total_saved_records"] += 1
                except Exception as exc:
                    page_failed += 1
                    checkpoint["total_failed_records"] += 1
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
                    existing_url_hashes.add(record["crawl"]["url_hash"])
                    if logger:
                        logger.error("[WHO Publications] detail failed: %s error=%s", detail_url, exc)

                _append_who_publication_record(record)
                records.append(record)
                checkpoint["last_finished_item_index"] = item_index
                time.sleep(0.8)

            checkpoint["last_finished_page"] = page_index
            checkpoint["last_finished_item_index"] = len(list_items)
            if logger:
                logger.info(
                    "[WHO Publications] page=%s parsed_items=%s saved=%s skipped_existing=%s failed=%s",
                    page_index,
                    len(list_items),
                    page_saved,
                    page_skipped,
                    page_failed,
                )
            if page_index % checkpoint_interval == 0:
                _save_checkpoint(site_config, checkpoint, logger)

        _save_checkpoint(site_config, checkpoint, logger)
    except KeyboardInterrupt:
        _save_checkpoint(site_config, checkpoint, logger)
        if logger:
            logger.warning("[WHO Publications] interrupted by user, checkpoint saved")
        print("已保存断点，可以下次继续运行")
    except Exception:
        _save_checkpoint(site_config, checkpoint, logger)
        if logger:
            logger.exception("[WHO Publications] severe error, checkpoint saved")
        raise

    save_doc_id_registry(registry)
    return records


def _checkpoint_path(site_config: dict[str, Any]) -> Path:
    configured = site_config.get("checkpoint_file", "data/checkpoints/who_publications_checkpoint.json")
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _empty_checkpoint(site_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "site": "who_publications",
        "channel_url": site_config.get("channel_url", ""),
        "crawl_mode": "full",
        "last_finished_page": 0,
        "last_finished_item_index": 0,
        "total_seen_items": 0,
        "total_saved_records": 0,
        "total_skipped_existing": 0,
        "total_failed_records": 0,
        "updated_at": "",
    }


def _load_checkpoint(site_config: dict[str, Any]) -> dict[str, Any]:
    path = _checkpoint_path(site_config)
    if not site_config.get("resume", True) or not path.exists():
        return _empty_checkpoint(site_config)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_checkpoint(site_config)
    checkpoint = _empty_checkpoint(site_config)
    checkpoint.update({key: loaded.get(key, value) for key, value in checkpoint.items()})
    return checkpoint


def _save_checkpoint(site_config: dict[str, Any], checkpoint: dict[str, Any], logger=None) -> None:
    if not site_config.get("save_checkpoint", True):
        return
    path = _checkpoint_path(site_config)
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    if logger:
        logger.info("[WHO Publications] checkpoint saved: page=%s", checkpoint.get("last_finished_page", 0))


def _load_existing_url_hashes(registry: dict[str, Any]) -> set[str]:
    existing: set[str] = set()
    if WHO_PUBLICATIONS_OUTPUT_PATH.exists():
        with WHO_PUBLICATIONS_OUTPUT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url_hash = record.get("crawl", {}).get("url_hash", "")
                if url_hash:
                    existing.add(url_hash)
                elif record.get("url"):
                    existing.add(hash_url(record["url"]))
    for key, entry in registry.items():
        if isinstance(entry, dict):
            if entry.get("url_hash"):
                existing.add(entry["url_hash"])
            else:
                existing.add(key)
    return existing


def _append_who_publication_record(record: dict[str, Any]) -> None:
    output_record = dict(record)
    output_record.pop("_already_saved", None)
    append_jsonl(output_record, WHO_PUBLICATIONS_OUTPUT_PATH)
    record["_already_saved"] = True


def _build_list_urls(first_url: str, max_pages: int) -> list[str]:
    if not first_url:
        return []
    if "/publications/i" in urlparse(first_url).path:
        urls = []
        for page_no in range(1, max_pages + 1):
            skip = (page_no - 1) * WHO_PUBLICATIONS_PAGE_SIZE
            query = urlencode(
                {
                    "$orderby": "PublicationDateAndTime desc",
                    "$top": str(WHO_PUBLICATIONS_PAGE_SIZE),
                    "$skip": str(skip),
                }
            )
            urls.append(f"{WHO_PUBLICATIONS_API_URL}&{query}")
        return urls
    urls = [first_url]
    for page_no in range(2, max_pages + 1):
        parsed = urlparse(first_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["page"] = str(page_no)
        urls.append(urlunparse(parsed._replace(query=urlencode(query))))
    return urls


def _parse_publication_list(html: str, base_url: str) -> list[dict[str, str]]:
    api_payload = _extract_publications_api_payload(html)
    if api_payload:
        return _parse_publication_api_list(api_payload)

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


def _looks_like_publications_api_response(text: str) -> bool:
    stripped = (text or "").lstrip()
    return stripped.startswith("{") and '"value"' in stripped and "PublicationDateAndTime" in stripped


def _extract_publications_api_payload(text: str) -> str:
    if _looks_like_publications_api_response(text):
        return text
    soup = BeautifulSoup(text or "", "lxml")
    body_text = soup.get_text("", strip=True)
    if _looks_like_publications_api_response(body_text):
        return body_text
    return ""


def _parse_publication_api_list(text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    items: list[dict[str, str]] = []
    for entry in payload.get("value", []):
        if not isinstance(entry, dict):
            continue
        slug = entry.get("UrlName") or str(entry.get("ItemDefaultUrl", "")).strip("/").split("/")[-1]
        if not slug:
            continue
        overview = entry.get("Overview") or entry.get("Summary") or ""
        items.append(
            {
                "title": clean_text(entry.get("Title", "")),
                "url": f"https://www.who.int/publications/i/item/{slug}",
                "publish_date": entry.get("FormatedDate", "") or _format_api_date(entry.get("PublicationDateAndTime", "")),
                "summary": clean_text(BeautifulSoup(overview, "lxml").get_text(" ", strip=True)),
            }
        )
    return items


def _format_api_date(value: str) -> str:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not match:
        return ""
    year, month, day = match.groups()
    month_names = {
        "01": "January",
        "02": "February",
        "03": "March",
        "04": "April",
        "05": "May",
        "06": "June",
        "07": "July",
        "08": "August",
        "09": "September",
        "10": "October",
        "11": "November",
        "12": "December",
    }
    return f"{int(day)} {month_names.get(month, month)} {year}"


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
    publication_details = parse_publication_details(html, detail_url)
    overview_node = _extract_overview_node(soup)
    summary = clean_text(overview_node.get_text(" ", strip=True)) if overview_node else ""
    body_html = str(main_node) if main_node else ""
    language = extract_language_versions(html, detail_url)

    return {
        "title": title,
        "publish_date": publish_date,
        "publication_type": publication_type,
        "summary": summary,
        "body_text": main_text,
        "body_html": body_html,
        "publication_details": publication_details,
        "language": language,
        "attachments": _extract_attachments(soup_for_links, detail_url),
        "images": _extract_images(main_node or soup_for_links, detail_url),
        "raw_source": "; ".join(publication_details.get("who_team", [])) or _extract_who_team(main_text),
    }


def _merge_detail(record: dict[str, Any], detail: dict[str, Any]) -> None:
    record["title"] = detail.get("title") or record.get("title", "")
    record["dates"]["publish_date"] = detail.get("publish_date") or record["dates"].get("publish_date", "")
    record["dates"]["publish_year"] = extract_publish_year(record["dates"]["publish_date"])
    record["content"]["body_text"] = detail.get("body_text", "")
    record["content"]["body_html"] = detail.get("body_html", "")
    record["content"]["summary"] = detail.get("summary") or record["content"].get("summary", "")
    record["content"]["abstract"] = record["content"]["summary"]
    record["attachments"] = detail.get("attachments", [])
    record["images"] = detail.get("images", [])
    record["publication_details"] = detail.get("publication_details", record.get("publication_details", {}))
    record["classification"]["document_type"] = detail.get("publication_type") or "publication"
    record["who_metadata"]["publication_type"] = detail.get("publication_type", "")
    teams = record.get("publication_details", {}).get("who_team", [])
    if teams and not record["organization"].get("who_programme"):
        record["organization"]["who_programme"] = teams[0]
        record["who_metadata"]["who_programme"] = teams[0]
    language = detail.get("language") or {}
    if language:
        record["language"].update(language)
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


def _process_language_versions(
    record: dict[str, Any],
    detail: dict[str, Any],
    site_config: dict[str, Any],
    fetch_html: FetchHtml,
    detail_url: str,
    timeout: int,
    logger=None,
) -> None:
    versions = record.get("language", {}).get("language_versions", [])
    if not versions:
        record["language"]["available_languages"] = ["English"]
        return

    should_crawl = site_config.get("crawl_language_versions", True)
    current_url = normalize_url(detail_url)
    filled_versions = []
    seen_urls = {current_url}
    for version in versions:
        version_url = version.get("url", "")
        normalized = normalize_url(version_url) if version_url else ""
        if not version_url or normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        if should_crawl:
            try:
                lang_html, _ = fetch_html(
                    version_url,
                    headers=WHO_HEADERS,
                    referer=detail_url,
                    timeout=timeout,
                )
                lang_detail = _parse_language_version_detail(lang_html, version_url)
                version.update(lang_detail)
                suffix = version.get("language_code") or safe_filename(version.get("language_name", "language"))
                save_raw_html(lang_html, record["doc_id"], "publications", suffix=suffix)
            except Exception as exc:
                if logger:
                    logger.warning(
                        "[WHO Publications] language version failed: %s error=%s",
                        version_url,
                        exc,
                    )
        filled_versions.append(version)

    available = ["English"]
    for version in filled_versions:
        name = version.get("language_name", "")
        if name and name not in available:
            available.append(name)
    record["language"]["original_language"] = "English"
    record["language"]["available_languages"] = available
    record["language"]["language_versions"] = filled_versions


def _parse_language_version_detail(html: str, url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()
    main_node = _find_main_node(soup)
    body_text = clean_text(main_node.get_text(" ", strip=True)) if main_node else clean_text(soup.get_text(" ", strip=True))
    body_html = str(main_node) if main_node else ""
    attachments = _extract_attachments(BeautifulSoup(html, "lxml"), url)
    download_url = attachments[0].get("url", "") if attachments else ""
    return {
        "title": _extract_title(soup),
        "body_text": body_text,
        "body_html": body_html,
        "download_url": download_url,
    }


def parse_publication_details(detail_html: str, detail_url: str = "") -> dict[str, Any]:
    """Parse WHO publication metadata labels from a detail page."""
    soup = BeautifulSoup(detail_html, "lxml")
    for node in soup(["script", "style", "noscript", "iframe"]):
        node.decompose()

    label_values = _extract_detail_label_values(soup)
    references = _parse_reference_numbers(label_values.get("reference numbers", ""))
    copyright_node = _find_label_value_node(soup, "copyright")
    license_url = ""
    if copyright_node:
        for a_tag in copyright_node.find_all("a", href=True):
            href = a_tag.get("href", "").strip()
            text = clean_text(a_tag.get_text(" ", strip=True)).lower()
            if "creative" in text or "license" in text or "creativecommons" in href.lower():
                license_url = urljoin(detail_url, href) if detail_url else href
                break

    return {
        "who_team": _split_people_or_teams(label_values.get("who team", "")),
        "editors": _split_people_or_teams(label_values.get("editors", "")),
        "number_of_pages": _extract_number_of_pages(label_values.get("number of pages", "")),
        "reference_numbers": references,
        "copyright": label_values.get("copyright", ""),
        "license_url": license_url,
    }


def extract_language_versions(html: str, base_url: str) -> dict[str, Any]:
    """Extract language switcher links without fetching the target pages."""
    soup = BeautifulSoup(html, "lxml")
    language_versions: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    available = ["English"]

    candidates = []
    for selector in (
        "[class*='language'] a[href]",
        "[id*='language'] a[href]",
        ".language-selector a[href]",
        "a[href*='/zh/']",
        "a[href*='/es/']",
        "a[href*='/fr/']",
        "a[href*='/ar/']",
        "a[href*='/ru/']",
        "a[href*='/pt/']",
    ):
        candidates.extend(soup.select(selector))

    for a_tag in candidates:
        language_name = clean_text(a_tag.get_text(" ", strip=True))
        if not language_name:
            continue
        language_code = _language_code(language_name, a_tag.get("href", ""))
        if not language_code and language_name.lower() not in LANGUAGE_CODE_MAP:
            continue
        full_url = urljoin(base_url, a_tag.get("href", "").strip())
        normalized = normalize_url(full_url)
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        if language_name not in available:
            available.append(language_name)
        if language_code == "en" and normalized == normalize_url(base_url):
            continue
        language_versions.append(
            {
                "language_code": language_code,
                "language_name": language_name,
                "url": full_url,
                "title": "",
                "body_text": "",
                "body_html": "",
                "download_url": "",
            }
        )

    return {
        "language_code": "en",
        "language_name": "English",
        "is_translation": False,
        "original_language": "English",
        "available_languages": available or ["English"],
        "language_versions": language_versions,
    }


def _extract_detail_label_values(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for label in DETAIL_LABELS:
        node = _find_label_value_node(soup, label)
        if node:
            values[label] = clean_text(node.get_text(" ", strip=True))
        else:
            values[label] = ""

    page_text = clean_text(soup.get_text(" ", strip=True))
    ordered_labels = "|".join(re.escape(label) for label in DETAIL_LABELS)
    for label in DETAIL_LABELS:
        if values[label]:
            continue
        pattern = rf"(?i)\b{re.escape(label)}\b\s+(.+?)(?=\b(?:{ordered_labels})\b|$)"
        match = re.search(pattern, page_text)
        if match:
            values[label] = clean_text(match.group(1))
    return values


def _find_label_value_node(soup: BeautifulSoup, label: str):
    label_re = re.compile(rf"^{re.escape(label)}$", re.IGNORECASE)
    for node in soup.find_all(string=label_re):
        parent = node.parent
        if not parent:
            continue
        container = parent.parent
        if container:
            siblings = []
            for sibling in parent.find_next_siblings():
                sibling_text = clean_text(sibling.get_text(" ", strip=True))
                if sibling_text.lower() in DETAIL_LABELS:
                    break
                siblings.append(str(sibling))
            if siblings:
                return BeautifulSoup("".join(siblings), "lxml")
            container_text = clean_text(container.get_text(" ", strip=True))
            if len(container_text) > len(label):
                cloned = BeautifulSoup(str(container), "lxml")
                first = cloned.find(string=label_re)
                if first:
                    first.extract()
                return cloned
    return None


def _split_people_or_teams(value: str) -> list[str]:
    text = clean_text(value)
    if not text:
        return []
    parts = re.split(r"\s*;\s*|\s*,\s*(?=[A-Z])|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def _extract_number_of_pages(value: str) -> str:
    match = re.search(r"\d+", value or "")
    return match.group(0) if match else clean_text(value)


def _parse_reference_numbers(value: str) -> dict[str, list[str]]:
    refs = {"isbn": [], "issn": [], "other": []}
    text = clean_text(value)
    if not text:
        return refs
    isbn_matches = re.findall(r"(?i)\bISBN(?:-1[03])?:?\s*([0-9Xx][0-9Xx\-\s]{8,20})", text)
    issn_matches = re.findall(r"(?i)\bISSN:?\s*([0-9Xx]{4}-?[0-9Xx]{4})", text)
    refs["isbn"] = _dedupe_clean(isbn_matches)
    refs["issn"] = _dedupe_clean(issn_matches)
    remainder = re.sub(r"(?i)\bISBN(?:-1[03])?:?\s*[0-9Xx][0-9Xx\-\s]{8,20}", "", text)
    remainder = re.sub(r"(?i)\bISSN:?\s*[0-9Xx]{4}-?[0-9Xx]{4}", "", remainder)
    refs["other"] = [clean_text(remainder)] if clean_text(remainder) else []
    return refs


def _dedupe_clean(values: list[str]) -> list[str]:
    seen = set()
    results = []
    for value in values:
        cleaned = clean_text(value).strip(" .;,")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            results.append(cleaned)
    return results


def _language_code(language_name: str, href: str = "") -> str:
    lowered = (language_name or "").strip().lower()
    if lowered in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[lowered]
    href_lower = (href or "").lower()
    for code in ("zh", "es", "fr", "ar", "ru", "pt", "en"):
        if f"/{code}/" in href_lower or href_lower.endswith(f"/{code}"):
            return code
    return ""


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
