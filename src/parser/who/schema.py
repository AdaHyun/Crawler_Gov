"""Unified WHO output schema helpers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


WHO_SITE_NAME = "World Health Organization"
WHO_SITE_DOMAIN = "who.int"
WHO_SITE_URL = "https://www.who.int"


def build_who_base_record(site_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a complete empty WHO record following the project schema."""
    site_config = site_config or {}
    now_date = datetime.now().strftime("%Y-%m-%d")
    channel_name = site_config.get("channel_name", "")
    channel_url = site_config.get("channel_url", "")

    record: dict[str, Any] = {
        "doc_id": "",
        "title": "",
        "url": "",
        "source": {
            "site_name": WHO_SITE_NAME,
            "site_domain": WHO_SITE_DOMAIN,
            "site_url": WHO_SITE_URL,
            "channel_name": channel_name,
            "channel_url": channel_url,
            "source_type": "international_organization",
            "data_source_type": site_config.get("data_source_type", ""),
        },
        "organization": {
            "source_department": "",
            "issuing_authority": [WHO_SITE_NAME],
            "joint_departments": [],
            "who_regional_office": "",
            "who_country_office": "",
            "who_programme": "",
        },
        "classification": {
            "policy_level": "",
            "document_type": "",
            "policy_category": "",
            "topic_tags": [],
            "target_region": "",
            "storage_categories": ["WHO", channel_name] if channel_name else ["WHO"],
            "health_topics": [],
            "disease_category": "",
            "emergency_category": "",
            "sdg_related": False,
        },
        "dates": {
            "publish_date": "",
            "crawl_date": now_date,
            "last_updated": "",
            "data_reference_year": "",
            "data_reference_period": "",
        },
        "content": {
            "body_text": "",
            "body_html": "",
            "summary": "",
            "abstract": "",
        },
        "attachments": [],
        "images": [],
        "language": {
            "language_code": site_config.get("language", "en"),
            "language_name": "English",
            "is_translation": False,
            "original_language": "",
            "available_languages": [],
        },
        "geo": {
            "who_region": "",
            "country": "",
            "country_iso3": "",
            "country_iso2": "",
            "target_region": "",
            "coverage_level": "",
        },
        "who_metadata": {
            "who_section": channel_name,
            "who_programme": "",
            "health_topics": [],
            "disease_names": [],
            "pathogen_names": [],
            "emergency_event": "",
            "sdg_goals": [],
            "sdg_targets": [],
            "publication_type": "",
            "series_name": "",
        },
        "indicator_data": {
            "is_indicator_record": False,
            "dataset_name": "",
            "indicator_code": "",
            "indicator_name": "",
            "indicator_description": "",
            "year": "",
            "period": "",
            "value": "",
            "numeric_value": None,
            "unit": "",
            "measure_type": "",
            "dimensions": {},
            "confidence_interval_lower": None,
            "confidence_interval_upper": None,
            "estimate_type": "",
            "data_source": "",
        },
        "api": {
            "is_api_source": False,
            "api_name": "",
            "api_url": "",
            "api_endpoint": "",
            "query_params": {},
            "response_format": "",
            "retrieved_at": "",
        },
        "crawl": {
            "crawler_name": site_config.get("crawler_name", "who_publications"),
            "crawl_status": "",
            "http_status": "",
            "raw_html_path": "",
            "url_hash": "",
            "text_hash": "",
            "error_message": "",
        },
        "raw": {
            "raw_title": "",
            "raw_date": "",
            "raw_source": "",
            "raw_json": {},
            "raw_api_response_path": "",
        },
    }
    return deepcopy(record)
