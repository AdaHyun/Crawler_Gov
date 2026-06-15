"""Reclassify downloaded attachment and image folders.

The script works at article-folder granularity:

    data/attachments/<site>/<channel>/<article>/
    data/images/<site>/<channel>/<article>/

Rules live in config/reclassify_rules.json. By default this is a dry run.
Use --apply to move folders and rewrite JSONL output files.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "output"
LOGS_DIR = DATA_DIR / "logs"
DEFAULT_RULES_PATH = BASE_DIR / "config" / "reclassify_rules.json"
MEDIA_ROOTS = ("attachments", "images")


INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|]')
STANDARD_CODE_RE = re.compile(r"^\s*(GB\s*[_/]?\s*T|WS\s*[_/]?\s*T|WST|GB|WS)(?=[\s._/-]*\d|\b)", re.IGNORECASE)


@dataclass
class MovePlan:
    media_type: str
    site: str
    channel: str
    article: str
    old_dir: Path
    new_dir: Path
    levels: list[str]
    reason: str
    status: str


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = INVALID_PATH_CHARS.sub("_", value or "").strip()
    return value or fallback


def load_rules(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    targets = data.get("targets")
    if not isinstance(targets, list):
        raise ValueError(f"{path} must contain a 'targets' list")
    return data


def normalize_standard_code(title: str) -> str:
    match = STANDARD_CODE_RE.search(title or "")
    if not match:
        return "未识别标准号"
    raw = re.sub(r"[\s/]+", "_", match.group(1).upper())
    raw = raw.replace("__", "_")
    if raw in {"WST", "WS_T"}:
        return "WS_T"
    if raw == "GB_T":
        return "GB_T"
    return raw


def keyword_group(title: str, groups: list[dict[str, Any]], default: str) -> tuple[str, str]:
    for group in groups:
        name = str(group.get("name", "")).strip()
        keywords = [str(k) for k in group.get("keywords", [])]
        if name and any(keyword and keyword in title for keyword in keywords):
            return name, f"keyword:{name}"
    return default, "default"


def classify_article(article: str, rule: dict[str, Any]) -> tuple[list[str], str]:
    classifier = rule.get("classifier", "keyword_groups")
    groups = rule.get("groups", [])
    default = str(rule.get("default", "未分类"))

    if classifier == "standard_topic":
        topic, reason = keyword_group(article, groups, default)
        levels = [topic]
        if rule.get("code_level", False):
            code = normalize_standard_code(article)
            levels.append(code)
            reason = f"{reason};standard_code:{code}"
        return levels, reason

    group, reason = keyword_group(article, groups, default)
    return [group], reason


def unique_destination(path: Path) -> tuple[Path, str]:
    if not path.exists():
        return path, "planned"
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name}__{index}")
        if not candidate.exists():
            return candidate, "planned_renamed"
    raise RuntimeError(f"Could not find a free destination for {path}")


def known_first_level_dirs(rule: dict[str, Any]) -> set[str]:
    names = {str(rule.get("default", "未分类"))}
    names.update(str(group.get("name", "")) for group in rule.get("groups", []))
    return {name for name in names if name}


def collect_move_plans(data_dir: Path, rules: dict[str, Any], media_types: list[str]) -> list[MovePlan]:
    plans: list[MovePlan] = []

    for rule in rules["targets"]:
        site = str(rule.get("site", ""))
        channel = str(rule.get("channel", ""))
        if not site or not channel:
            continue

        first_level_dirs = known_first_level_dirs(rule)
        for media_type in media_types:
            channel_dir = data_dir / media_type / site / channel
            if not channel_dir.exists():
                plans.append(
                    MovePlan(media_type, site, channel, "", channel_dir, channel_dir, [], "missing_channel", "missing")
                )
                continue

            for article_dir in sorted(p for p in channel_dir.iterdir() if p.is_dir()):
                if article_dir.name in first_level_dirs:
                    continue
                levels, reason = classify_article(article_dir.name, rule)
                safe_levels = [safe_name(level, "未分类") for level in levels]
                target_parent = channel_dir.joinpath(*safe_levels)
                desired = target_parent / article_dir.name

                if desired.resolve() == article_dir.resolve():
                    status = "unchanged"
                    destination = desired
                else:
                    destination, status = unique_destination(desired)

                plans.append(
                    MovePlan(
                        media_type=media_type,
                        site=site,
                        channel=channel,
                        article=article_dir.name,
                        old_dir=article_dir,
                        new_dir=destination,
                        levels=safe_levels,
                        reason=reason,
                        status=status,
                    )
                )

    return plans


def build_file_mapping(plans: list[MovePlan], base_dir: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for plan in plans:
        if plan.status not in {"planned", "planned_renamed"}:
            continue
        if not plan.old_dir.exists():
            continue
        for old_file in plan.old_dir.rglob("*"):
            if not old_file.is_file():
                continue
            rel_inside = old_file.relative_to(plan.old_dir)
            new_file = plan.new_dir / rel_inside
            old_rel = old_file.relative_to(base_dir).as_posix()
            new_rel = new_file.relative_to(base_dir).as_posix()
            mapping[old_rel] = new_rel
    return mapping


def write_report(plans: list[MovePlan], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["media_type", "site", "channel", "article", "levels", "old_dir", "new_dir", "reason", "status"])
        for plan in plans:
            writer.writerow(
                [
                    plan.media_type,
                    plan.site,
                    plan.channel,
                    plan.article,
                    "/".join(plan.levels),
                    plan.old_dir.as_posix(),
                    plan.new_dir.as_posix(),
                    plan.reason,
                    plan.status,
                ]
            )


def move_dirs(plans: list[MovePlan]) -> None:
    for plan in plans:
        if plan.status not in {"planned", "planned_renamed"}:
            continue
        plan.new_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plan.old_dir), str(plan.new_dir))


def remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def update_local_paths(records: list[dict[str, Any]], file_mapping: dict[str, str]) -> int:
    changed = 0
    for record in records:
        for field in ("attachments", "images"):
            items = record.get(field)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                local_path = item.get("local_path")
                if not isinstance(local_path, str):
                    continue
                normalized = local_path.replace("\\", "/")
                new_path = file_mapping.get(normalized)
                if new_path and new_path != local_path:
                    item["local_path"] = new_path
                    changed += 1
    return changed


def categories_from_local_path(local_path: str, rules: dict[str, Any]) -> list[str] | None:
    parts = Path(local_path.replace("\\", "/")).parts
    if len(parts) < 6:
        return None
    if parts[0] != "data" or parts[1] not in MEDIA_ROOTS:
        return None

    site = parts[2]
    channel = parts[3]
    for rule in rules.get("targets", []):
        if rule.get("site") != site or rule.get("channel") != channel:
            continue

        known_levels = known_first_level_dirs(rule)
        if len(parts) >= 7 and parts[4] in known_levels:
            # data/<media>/<site>/<channel>/<level...>/<article>/<file>
            return [channel, *parts[4:-2]]
    return None


def categories_from_record(site: str, channel: str, title: str, rules: dict[str, Any]) -> list[str] | None:
    for rule in rules.get("targets", []):
        if rule.get("site") != site or rule.get("channel") != channel:
            continue
        levels, _ = classify_article(title, rule)
        return [channel, *levels]
    return None


def update_storage_categories(records: list[dict[str, Any]], plans: list[MovePlan], rules: dict[str, Any]) -> int:
    article_index: dict[tuple[str, str, str], list[str]] = {}
    for plan in plans:
        if plan.article and plan.levels and plan.media_type == "attachments":
            article_index[(plan.site, plan.channel, plan.article)] = [plan.channel, *plan.levels]

    changed = 0
    for record in records:
        categories = None
        for field in ("attachments", "images"):
            items = record.get(field)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                local_path = item.get("local_path")
                if not isinstance(local_path, str):
                    continue
                categories = categories_from_local_path(local_path, rules)
                if categories:
                    break
            if categories:
                break

        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        site = str(source.get("site_name", ""))
        channel = str(source.get("channel_name", ""))
        title = str(record.get("title", ""))
        categories = categories or categories_from_record(site, channel, title, rules)
        categories = categories or article_index.get((site, channel, title))
        if not categories:
            continue

        classification = record.setdefault("classification", {})
        if isinstance(classification, dict) and classification.get("storage_categories") != categories:
            classification["storage_categories"] = categories
            changed += 1
    return changed


def rewrite_jsonl_outputs(
    output_dir: Path,
    plans: list[MovePlan],
    file_mapping: dict[str, str],
    rules: dict[str, Any],
    apply: bool,
) -> dict[str, int]:
    stats = {"files": 0, "records": 0, "path_updates": 0, "category_updates": 0}
    jsonl_files = sorted(output_dir.glob("*.jsonl"))

    for path in jsonl_files:
        records: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        file_changed = False

        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    raw_lines.append(line)
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    raw_lines.append(line)

        path_updates = update_local_paths(records, file_mapping)
        category_updates = update_storage_categories(records, plans, rules)
        file_changed = path_updates > 0 or category_updates > 0

        stats["files"] += 1
        stats["records"] += len(records)
        stats["path_updates"] += path_updates
        stats["category_updates"] += category_updates

        if apply and file_changed:
            backup_path = path.with_suffix(path.suffix + f".bak_{datetime.now():%Y%m%d_%H%M%S}")
            shutil.copy2(path, backup_path)
            with path.open("w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                for raw_line in raw_lines:
                    f.write(raw_line)

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reclassify saved crawler attachment/image folders.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH, help="Path to reclassification rules JSON.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Crawler data directory.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="JSONL output directory.")
    parser.add_argument("--media", nargs="+", choices=MEDIA_ROOTS, default=list(MEDIA_ROOTS), help="Media roots to process.")
    parser.add_argument("--report", type=Path, default=None, help="CSV report path.")
    parser.add_argument("--apply", action="store_true", help="Actually move folders and rewrite JSONL.")
    parser.add_argument("--no-jsonl", action="store_true", help="Do not update JSONL output files.")
    parser.add_argument("--clean-empty", action="store_true", help="Remove empty directories after --apply.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rules = load_rules(args.rules)
    report_path = args.report or LOGS_DIR / f"reclassify_saved_files_{datetime.now():%Y%m%d_%H%M%S}.csv"

    plans = collect_move_plans(args.data_dir, rules, args.media)
    file_mapping = build_file_mapping(plans, BASE_DIR)
    write_report(plans, report_path)

    planned_count = sum(1 for plan in plans if plan.status in {"planned", "planned_renamed"})
    missing_count = sum(1 for plan in plans if plan.status == "missing")
    renamed_count = sum(1 for plan in plans if plan.status == "planned_renamed")

    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Move plans: {planned_count} planned, {renamed_count} need renamed destination, {missing_count} missing channels")
    print(f"File path mappings: {len(file_mapping)}")
    print(f"Report: {report_path}")

    if not args.no_jsonl:
        jsonl_stats = rewrite_jsonl_outputs(args.output_dir, plans, file_mapping, rules, apply=args.apply)
        print(
            "JSONL: "
            f"{jsonl_stats['files']} files, {jsonl_stats['records']} records, "
            f"{jsonl_stats['path_updates']} local_path updates, "
            f"{jsonl_stats['category_updates']} storage_categories updates"
        )

    if args.apply:
        move_dirs(plans)
        if args.clean_empty:
            for media_type in args.media:
                remove_empty_dirs(args.data_dir / media_type)
        print("Applied reclassification.")
    else:
        print("Dry run only. Re-run with --apply to move folders and rewrite JSONL.")


if __name__ == "__main__":
    main()
