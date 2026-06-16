"""Shared helpers for WHO parsers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from utils import PROJECT_ROOT


REGISTRY_PATH = PROJECT_ROOT / "data" / "id_registry" / "who_doc_id_registry.json"
WHO_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}
CHANNEL_CODES = {
    "Publications": "PUB",
    "Disease Outbreak News": "DON",
    "Fact sheets": "FS",
    "Health topics": "HT",
    "Global Health Observatory": "GHO",
}
EXTENSION_TYPE_MAP = {
    "pdf": "pdf",
    "xlsx": "excel",
    "xls": "excel",
    "csv": "csv",
    "doc": "word",
    "docx": "word",
    "zip": "zip",
}
FILE_TYPE_EXTENSION_MAP = {
    "pdf": "pdf",
    "excel": "xlsx",
    "csv": "csv",
    "word": "docx",
    "zip": "zip",
    "unknown": "bin",
    "iris_record": "bin",
}
MEANINGLESS_URL_NAMES = {
    "",
    "content",
    "download",
    "bitstream",
    "bitstreams",
    "handle",
    "handles",
    "item",
    "items",
}


def normalize_url(url: str) -> str:
    """Build a canonical URL for dedupe and registry lookup."""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(("utm_", "fbclid", "gclid"))
    ]
    query = urlencode(sorted(query_pairs))
    return urlunparse((scheme, netloc, path, "", query, ""))


def hash_url(url: str) -> str:
    return hashlib.md5(normalize_url(url).encode("utf-8")).hexdigest()


def hash_text(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest() if text else ""


def load_doc_id_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_doc_id_registry(registry: dict[str, Any], path: Path = REGISTRY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_publish_date(publish_date: str) -> str:
    text = (publish_date or "").strip()
    if not text:
        return "unknown-date"

    for fmt in ("%d %B %Y", "%B %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            pass

    match = re.search(r"(20\d{2}|19\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{year}{int(month):02d}{int(day):02d}"
    return "unknown-date"


def extract_publish_year(publish_date: str) -> str:
    """Extract a four-digit year from WHO date text."""
    date_code = normalize_publish_date(publish_date)
    if re.match(r"^\d{8}$", date_code):
        return date_code[:4]
    match = re.search(r"(20\d{2}|19\d{2})", publish_date or "")
    return match.group(1) if match else ""


def generate_doc_id(
    url: str,
    publish_date: str,
    site_config: dict[str, Any],
    batch_no: int = 1,
    registry: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Generate or reuse a stable WHO doc_id for a canonical URL."""
    registry = registry if registry is not None else load_doc_id_registry()
    canonical_url = normalize_url(url)
    url_hash = hash_url(canonical_url)
    existing = registry.get(url_hash)

    source_code = "WHO"
    channel_code = CHANNEL_CODES.get(site_config.get("channel_name", ""), "WHO")
    category_code = get_record_category_code(record or {}, site_config)
    date_code = normalize_publish_date(publish_date)
    batch_code = f"{max(batch_no, 1):02d}"
    prefix = f"{source_code}-{channel_code}-{category_code}-{date_code}-{batch_code}"

    if (
        isinstance(existing, dict)
        and existing.get("doc_id")
        and existing.get("category_code") == category_code
        and re.match(rf"^{re.escape(prefix)}-\d{{4}}$", existing.get("doc_id", ""))
    ):
        existing["title"] = (record or {}).get("title", "") or existing.get("title", "")
        existing["article_title"] = existing["title"]
        return existing["doc_id"], url_hash

    used_ids = {entry.get("doc_id") for entry in registry.values() if isinstance(entry, dict)}
    next_item_no = 1
    for entry in registry.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("source_code") != source_code:
            continue
        if entry.get("channel_code") != channel_code:
            continue
        if entry.get("category_code") != category_code:
            continue
        if entry.get("publish_date") != date_code:
            continue
        doc_id = entry.get("doc_id", "")
        match = re.match(rf"^{re.escape(prefix)}-(\d{{4}})$", doc_id)
        if match:
            next_item_no = max(next_item_no, int(match.group(1)) + 1)

    while True:
        sequence_id = f"{batch_code}-{next_item_no:04d}"
        doc_id = f"{source_code}-{channel_code}-{category_code}-{date_code}-{sequence_id}"
        if doc_id not in used_ids:
            break
        next_item_no += 1

    registry[url_hash] = {
        "url": url,
        "canonical_url": canonical_url,
        "url_hash": url_hash,
        "doc_id": doc_id,
        "title": (record or {}).get("title", ""),
        "article_title": (record or {}).get("title", ""),
        "source_code": source_code,
        "channel_code": channel_code,
        "category_code": category_code,
        "publish_date": date_code,
        "sequence_id": sequence_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return doc_id, url_hash


def get_record_category_code(record: dict[str, Any], site_config: dict[str, Any] | None = None) -> str:
    """Infer a safe category code for WHO doc_id and file names."""
    candidates: list[Any] = [
        get_nested_value(record, "classification.document_type"),
        get_nested_value(record, "who_metadata.publication_type"),
        get_nested_value(record, "classification.policy_category"),
        _first_non_empty(get_nested_value(record, "classification.health_topics")),
        _first_non_empty(get_nested_value(record, "classification.topic_tags")),
        _last_non_empty(get_nested_value(record, "classification.storage_categories")),
    ]
    for candidate in candidates:
        cleaned = clean_category_code(candidate)
        if cleaned != "unknown":
            return cleaned
    return "unknown"


def clean_category_code(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    text = re.sub(r'[\\/:*?"<>|]+', "-", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        return "unknown"
    return text[:40].strip("-") or "unknown"


def _first_non_empty(value: Any) -> str:
    if isinstance(value, list):
        return next((str(item).strip() for item in value if str(item).strip()), "")
    return str(value or "").strip()


def _last_non_empty(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items[-1] if items else ""
    return str(value or "").strip()


def save_raw_html(
    html: str,
    doc_id: str,
    channel_code: str = "publications",
    suffix: str = "",
) -> str:
    """Save WHO raw HTML below data/raw_html/who/{channel_code}/."""
    safe_doc_id = safe_filename(doc_id)
    safe_suffix = safe_filename(suffix) if suffix else ""
    target_dir = PROJECT_ROOT / "data" / "raw_html" / "who" / safe_filename(channel_code.lower())
    target_dir.mkdir(parents=True, exist_ok=True)
    file_stem = f"{safe_doc_id}_{safe_suffix}" if safe_suffix else safe_doc_id
    file_path = target_dir / f"{file_stem}.html"
    file_path.write_text(html or "", encoding="utf-8")
    return file_path.relative_to(PROJECT_ROOT).as_posix()


def infer_file_type(url: str, link_text: str = "", mime_type: str = "", filename: str = "") -> str:
    mime = (mime_type or "").lower()
    text = (link_text or "").lower()
    lower_url = (url or "").lower()
    lower_filename = (filename or "").lower()

    if "application/pdf" in mime:
        return "pdf"
    if any(token in mime for token in ("excel", "spreadsheet", "xlsx", "xls")):
        return "excel"
    if "text/csv" in mime or "csv" in mime:
        return "csv"
    if any(token in mime for token in ("word", "msword", "officedocument.wordprocessingml")):
        return "word"
    if "zip" in mime:
        return "zip"

    filename_suffix = Path(lower_filename).suffix.lower().lstrip(".")
    if filename_suffix in EXTENSION_TYPE_MAP:
        return EXTENSION_TYPE_MAP[filename_suffix]

    suffix_match = re.search(r"\.([a-z0-9]{2,5})(?:$|\?)", lower_url)
    if suffix_match and suffix_match.group(1) in EXTENSION_TYPE_MAP:
        return EXTENSION_TYPE_MAP[suffix_match.group(1)]

    if "pdf" in text:
        return "pdf"
    if any(token in text for token in ("excel", "xlsx", "xls")):
        return "excel"
    if "csv" in text:
        return "csv"
    if any(token in text for token in ("doc", "docx", "word")):
        return "word"
    if "zip" in text:
        return "zip"

    if (
        ("/server/api/core/bitstreams/" in lower_url and "/content" in lower_url)
        or ("/bitstreams/" in lower_url and "/download" in lower_url)
    ) and "download" in text:
        return "pdf"

    parsed = urlparse(lower_url)
    if parsed.netloc == "iris.who.int" and (
        parsed.path.startswith("/handle/") or parsed.path.startswith("/items/")
    ):
        return "iris_record"
    return "unknown"


def is_final_file_url(url: str) -> bool:
    lower_url = (url or "").lower()
    if re.search(r"\.(pdf|xlsx|xls|csv|docx|doc|zip)(?:$|\?)", lower_url):
        return True
    if "/bitstreams/" in lower_url and "/download" in lower_url:
        return True
    if "/server/api/core/bitstreams/" in lower_url and "/content" in lower_url:
        return True
    return False


def is_iris_landing_url(url: str) -> bool:
    parsed = urlparse((url or "").lower())
    return parsed.netloc == "iris.who.int" and (
        parsed.path.startswith("/handle/") or parsed.path.startswith("/items/")
    )


def resolve_iris_attachment(landing_url: str, timeout: int = 15) -> str:
    try:
        response = requests.get(landing_url, headers=WHO_HEADERS, timeout=timeout)
        response.raise_for_status()
    except Exception:
        return ""

    soup = BeautifulSoup(response.text, "lxml")
    candidates: list[str] = []
    for a_tag in soup.find_all("a", href=True):
        full_url = urljoin(landing_url, a_tag.get("href", "").strip())
        lower = full_url.lower()
        if ".pdf" in lower:
            candidates.append(full_url)
        elif "/bitstreams/" in lower and "/download" in lower:
            candidates.append(full_url)
        elif "/server/api/core/bitstreams/" in lower and "/content" in lower:
            candidates.append(full_url)
    return candidates[0] if candidates else ""


def head_attachment_url(url: str, timeout: int = 10) -> dict[str, str]:
    result = {"url": url, "mime_type": "", "file_size": "", "filename": ""}
    try:
        response = requests.head(url, headers=WHO_HEADERS, allow_redirects=True, timeout=timeout)
        if response.status_code in {403, 405}:
            return result
        response.raise_for_status()
    except Exception:
        return result

    result["url"] = response.url or url
    result["mime_type"] = response.headers.get("Content-Type", "").split(";")[0].strip()
    result["file_size"] = response.headers.get("Content-Length", "").strip()
    result["filename"] = parse_content_disposition_filename(response.headers.get("Content-Disposition", ""))
    return result


def enrich_attachment(attachment: dict[str, str], site_config: dict[str, Any]) -> dict[str, str]:
    link_text = attachment.get("name", "")
    original_url = attachment.get("url", "")
    landing_url = original_url if is_iris_landing_url(original_url) else attachment.get("landing_url", "")
    final_file_url = original_url if is_final_file_url(original_url) else attachment.get("final_file_url", "")
    status = "link_only"

    if landing_url and not final_file_url and site_config.get("resolve_iris_final_file", True):
        resolved_url = resolve_iris_attachment(landing_url)
        if resolved_url:
            final_file_url = resolved_url
            status = "resolved"

    current_url = final_file_url or landing_url or original_url
    mime_type = attachment.get("mime_type", "")
    file_size = attachment.get("file_size", "")
    original_filename = attachment.get("original_filename", "")

    if site_config.get("use_head_for_attachments", True) and current_url:
        head_info = head_attachment_url(current_url)
        if head_info.get("url") and head_info["url"] != current_url and is_final_file_url(head_info["url"]):
            final_file_url = head_info["url"]
            current_url = final_file_url
            status = "resolved"
        mime_type = head_info.get("mime_type") or mime_type
        file_size = head_info.get("file_size") or file_size
        original_filename = head_info.get("filename") or original_filename

    original_filename = infer_original_filename(
        url=current_url,
        link_text=link_text,
        file_type=attachment.get("file_type", ""),
        mime_type=mime_type,
        header_filename=original_filename,
    )
    file_type = infer_file_type(
        current_url,
        link_text=link_text,
        mime_type=mime_type,
        filename=original_filename,
    )
    if landing_url and not final_file_url:
        file_type = "iris_record"

    display_name = original_filename or filename_from_link_text(link_text)
    attachment.update(
        {
            "name": display_name,
            "url": final_file_url or landing_url or current_url,
            "landing_url": landing_url,
            "final_file_url": final_file_url,
            "file_type": file_type,
            "mime_type": mime_type,
            "file_size": file_size,
            "local_path": attachment.get("local_path", ""),
            "download_status": status,
            "file_hash": attachment.get("file_hash", ""),
            "original_filename": original_filename,
        }
    )
    return attachment


def build_attachment_local_path(
    record: dict[str, Any],
    attachment: dict[str, str],
    site_config: dict[str, Any],
    attachment_index: int = 1,
) -> Path:
    save_config = site_config.get("attachment_save", {})
    base_dir = PROJECT_ROOT / save_config.get("base_dir", "data/attachments/who/publications")
    folder_name = build_group_folder_name(record, site_config, save_config)
    target_dir = base_dir / folder_name
    file_name = build_local_attachment_filename(record, attachment, attachment_index)
    candidate = target_dir / file_name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        next_candidate = target_dir / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def build_image_local_path(
    record: dict[str, Any],
    image: dict[str, str],
    site_config: dict[str, Any],
    image_index: int = 1,
) -> Path:
    save_config = site_config.get("image_save", {})
    base_dir = PROJECT_ROOT / save_config.get("base_dir", "data/images/who/publications")
    folder_name = build_group_folder_name(record, site_config, save_config)
    target_dir = base_dir / folder_name
    file_name = build_local_image_filename(record, image, image_index)
    candidate = target_dir / file_name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        next_candidate = target_dir / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def download_attachment(
    record: dict[str, Any],
    attachment: dict[str, str],
    site_config: dict[str, Any],
    attachment_index: int = 1,
    timeout: int = 30,
) -> dict[str, str]:
    if not site_config.get("download_attachments", False):
        attachment["local_path"] = ""
        return attachment

    final_url = attachment.get("final_file_url", "")
    if not final_url:
        attachment["download_status"] = "failed"
        return attachment

    local_path = build_attachment_local_path(record, attachment, site_config, attachment_index)
    migrated_path = migrate_existing_attachment_to_group(record, attachment, site_config, attachment_index, local_path)
    if migrated_path is not None:
        attachment["local_path"] = migrated_path.relative_to(PROJECT_ROOT).as_posix()
        attachment["download_status"] = "downloaded"
        return attachment
    attachment["local_path"] = local_path.relative_to(PROJECT_ROOT).as_posix()
    try:
        try:
            response = requests.get(final_url, headers=WHO_HEADERS, stream=True, timeout=timeout)
        except requests.exceptions.SSLError:
            response = requests.get(final_url, headers=WHO_HEADERS, stream=True, timeout=timeout, verify=False)

        with response:
            response.raise_for_status()
            get_filename = parse_content_disposition_filename(response.headers.get("Content-Disposition", ""))
            get_mime_type = response.headers.get("Content-Type", "").split(";")[0].strip()
            get_file_size = response.headers.get("Content-Length", "").strip()
            if get_filename:
                attachment["original_filename"] = get_filename
                attachment["name"] = get_filename
            if get_mime_type:
                attachment["mime_type"] = get_mime_type
            if get_file_size:
                attachment["file_size"] = get_file_size
            attachment["file_type"] = infer_file_type(
                response.url or final_url,
                link_text=attachment.get("name", ""),
                mime_type=attachment.get("mime_type", ""),
                filename=attachment.get("original_filename", ""),
            )
            if response.url and response.url != final_url and is_final_file_url(response.url):
                attachment["final_file_url"] = response.url
                attachment["url"] = response.url

            local_path = build_attachment_local_path(record, attachment, site_config, attachment_index)
            attachment["local_path"] = local_path.relative_to(PROJECT_ROOT).as_posix()
            local_path.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.md5()
            with local_path.open("wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    hasher.update(chunk)
                    f.write(chunk)
        attachment["local_path"] = local_path.relative_to(PROJECT_ROOT).as_posix()
        attachment["download_status"] = "downloaded"
        attachment["file_hash"] = hasher.hexdigest()
    except Exception as exc:
        attachment["download_status"] = "failed"
        attachment["error_message"] = str(exc)
    return attachment


def migrate_existing_attachment_to_group(
    record: dict[str, Any],
    attachment: dict[str, str],
    site_config: dict[str, Any],
    attachment_index: int,
    target_path: Path,
) -> Path | None:
    """Move an already downloaded WHO attachment from an old group folder to the current group."""
    if target_path.exists():
        return target_path

    save_config = site_config.get("attachment_save", {})
    base_dir = PROJECT_ROOT / save_config.get("base_dir", "data/attachments/who/publications")
    expected_name = build_local_attachment_filename(record, attachment, attachment_index)
    if not base_dir.exists():
        return None
    matches = [path for path in base_dir.rglob(expected_name) if path.is_file()]
    if not matches:
        return None
    source_path = matches[0]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.replace(target_path)
    return target_path


def get_nested_value(record: dict[str, Any], dotted_path: str):
    current: Any = record
    for key in (dotted_path or "").split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def normalize_group_value(value: Any, unknown_folder: str) -> str:
    if isinstance(value, list):
        value = next((item for item in value if str(item).strip()), "")
    if value is None or value == "":
        value = unknown_folder
    return clean_category_code(value) if str(value).strip() else unknown_folder


def build_group_folder_name(
    record: dict[str, Any],
    site_config: dict[str, Any],
    save_config: dict[str, Any],
) -> str:
    unknown_folder = save_config.get("unknown_folder", "unknown")
    group_value = get_nested_value(record, save_config.get("group_by", ""))
    folder_name = normalize_group_value(group_value, unknown_folder)
    if folder_name == clean_category_code(unknown_folder):
        folder_name = get_record_category_code(record, site_config)
    if not folder_name or folder_name == "unknown":
        folder_name = clean_category_code(unknown_folder)
    return safe_filename(folder_name.lower(), max_length=80) or unknown_folder


def safe_filename(value: str, max_length: int = 120) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value or "").strip().strip(".")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:max_length] or "unknown"


def parse_content_disposition_filename(content_disposition: str) -> str:
    header = content_disposition or ""
    match = re.search(r"filename\*\s*=\s*([^;]+)", header, flags=re.IGNORECASE)
    if match:
        value = match.group(1).strip().strip('"')
        if "''" in value:
            value = value.split("''", 1)[1]
        return safe_original_filename(unquote(value))

    match = re.search(r'filename\s*=\s*"([^"]+)"', header, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"filename\s*=\s*([^;]+)", header, flags=re.IGNORECASE)
    if match:
        return safe_original_filename(unquote(match.group(1).strip().strip('"')))
    return ""


def infer_original_filename(
    url: str,
    link_text: str = "",
    file_type: str = "",
    mime_type: str = "",
    header_filename: str = "",
) -> str:
    for candidate in (
        header_filename,
        filename_from_url(url),
        filename_from_link_text(link_text),
    ):
        cleaned = safe_original_filename(candidate)
        if cleaned:
            return ensure_extension(cleaned, file_type=file_type, mime_type=mime_type, url=url, link_text=link_text)
    return ""


def filename_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    segment = unquote(Path(parsed.path).name)
    if segment.lower() in MEANINGLESS_URL_NAMES:
        return ""
    if "." not in segment:
        return ""
    return segment


def filename_from_link_text(link_text: str) -> str:
    text = clean_attachment_text(link_text)
    if not text:
        return ""
    if text.lower() in MEANINGLESS_URL_NAMES or text.lower().startswith("download"):
        return ""
    return text


def build_local_attachment_filename(
    record: dict[str, Any],
    attachment: dict[str, str],
    attachment_index: int = 1,
    max_length: int = 180,
) -> str:
    doc_id = safe_filename(record.get("doc_id", "WHO-ATT"))
    prefix = f"{doc_id}-att{attachment_index:02d}"
    original = infer_original_filename(
        url=attachment.get("final_file_url") or attachment.get("url", ""),
        link_text=attachment.get("name", ""),
        file_type=attachment.get("file_type", ""),
        mime_type=attachment.get("mime_type", ""),
        header_filename=attachment.get("original_filename", ""),
    )
    extension = extension_for_attachment(attachment)

    if not original:
        return f"{prefix}.{extension}"

    original = ensure_extension(original, file_type=attachment.get("file_type", ""), mime_type=attachment.get("mime_type", ""))
    original_path = Path(original)
    stem = safe_filename(original_path.stem, max_length=120)
    suffix = original_path.suffix.lower() or f".{extension}"
    filename = f"{prefix}-{stem}{suffix}"
    if len(filename) <= max_length:
        return filename
    keep = max_length - len(prefix) - len(suffix) - 1
    return f"{prefix}-{stem[:max(20, keep)]}{suffix}"


def build_local_image_filename(
    record: dict[str, Any],
    image: dict[str, str],
    image_index: int = 1,
    max_length: int = 180,
) -> str:
    doc_id = safe_filename(record.get("doc_id", "WHO-IMG"))
    url = image.get("url", "")
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
        suffix = ".jpg"
    filename = f"{doc_id}-img{image_index:02d}{suffix}"
    return filename[:max_length]


def extension_for_attachment(attachment: dict[str, str]) -> str:
    file_type = attachment.get("file_type") or infer_file_type(
        attachment.get("final_file_url") or attachment.get("url", ""),
        link_text=attachment.get("name", ""),
        mime_type=attachment.get("mime_type", ""),
        filename=attachment.get("original_filename", ""),
    )
    return FILE_TYPE_EXTENSION_MAP.get(file_type, "bin")


def ensure_extension(
    filename: str,
    file_type: str = "",
    mime_type: str = "",
    url: str = "",
    link_text: str = "",
) -> str:
    cleaned = safe_original_filename(filename)
    if not cleaned:
        return ""
    suffix = Path(cleaned).suffix.lower().lstrip(".")
    if suffix in EXTENSION_TYPE_MAP:
        return cleaned
    inferred = file_type if file_type and file_type != "unknown" else infer_file_type(
        url,
        link_text=link_text,
        mime_type=mime_type,
        filename=cleaned,
    )
    extension = FILE_TYPE_EXTENSION_MAP.get(inferred, "bin")
    return f"{cleaned}.{extension}"


def safe_original_filename(value: str, max_length: int = 140) -> str:
    cleaned = clean_attachment_text(value)
    if not cleaned:
        return ""
    path_name = Path(cleaned).name
    cleaned = safe_filename(path_name, max_length=max_length)
    if cleaned.lower() in MEANINGLESS_URL_NAMES:
        return ""
    return cleaned


def clean_attachment_text(value: str) -> str:
    text = unquote(value or "").strip()
    text = re.sub(r"\([^)]*(?:KB|MB|GB)[^)]*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?i)^download\s*(?:file|pdf|publication)?\s*", "", text).strip()
    text = text.strip("-_ .")
    return text
