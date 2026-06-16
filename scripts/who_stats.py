"""Export lightweight statistics for WHO JSONL outputs."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
WHO_OUTPUT_DIR = BASE_DIR / "data" / "output" / "who"
WHO_LOG_DIR = BASE_DIR / "data" / "logs" / "who"


def get_nested(record: dict, *keys, default=""):
    cur = record
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def as_list(value):
    return value if isinstance(value, list) else []


def read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                yield {"_parse_error": f"{path.name}:{line_no}: {exc}"}


def build_stats() -> dict:
    stats = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": str(WHO_OUTPUT_DIR.relative_to(BASE_DIR)),
        "files": {},
        "totals": {
            "records": 0,
            "attachments": 0,
            "images": 0,
            "parse_errors": 0,
        },
    }

    for jsonl_path in sorted(WHO_OUTPUT_DIR.glob("*.jsonl")):
        counters = {
            "records": 0,
            "attachments": 0,
            "images": 0,
            "parse_errors": 0,
            "site_name": Counter(),
            "channel_name": Counter(),
            "data_source_type": Counter(),
            "document_type": Counter(),
            "topic_tags": Counter(),
            "health_topics": Counter(),
            "publish_date": Counter(),
            "crawl_date": Counter(),
            "language_code": Counter(),
            "coverage_level": Counter(),
            "crawl_status": Counter(),
        }

        for record in read_jsonl(jsonl_path):
            if "_parse_error" in record:
                counters["parse_errors"] += 1
                continue

            counters["records"] += 1
            counters["attachments"] += len(as_list(record.get("attachments")))
            counters["images"] += len(as_list(record.get("images")))
            counters["site_name"][get_nested(record, "source", "site_name")] += 1
            counters["channel_name"][get_nested(record, "source", "channel_name")] += 1
            counters["data_source_type"][get_nested(record, "source", "data_source_type")] += 1
            counters["document_type"][get_nested(record, "classification", "document_type")] += 1
            counters["publish_date"][get_nested(record, "dates", "publish_date")] += 1
            counters["crawl_date"][get_nested(record, "dates", "crawl_date")] += 1
            counters["language_code"][get_nested(record, "language", "language_code")] += 1
            counters["coverage_level"][get_nested(record, "geo", "coverage_level")] += 1
            counters["crawl_status"][get_nested(record, "crawl", "crawl_status")] += 1

            for tag in as_list(get_nested(record, "classification", "topic_tags", default=[])):
                counters["topic_tags"][tag] += 1
            for topic in as_list(get_nested(record, "classification", "health_topics", default=[])):
                counters["health_topics"][topic] += 1

        stats["files"][jsonl_path.name] = {
            key: dict(value) if isinstance(value, Counter) else value
            for key, value in counters.items()
        }
        stats["totals"]["records"] += counters["records"]
        stats["totals"]["attachments"] += counters["attachments"]
        stats["totals"]["images"] += counters["images"]
        stats["totals"]["parse_errors"] += counters["parse_errors"]

    return stats


def main() -> None:
    WHO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    report_path = WHO_LOG_DIR / f"who_stats_{datetime.now():%Y%m%d_%H%M%S}.json"
    stats = build_stats()
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"WHO stats: {report_path}")


if __name__ == "__main__":
    main()

