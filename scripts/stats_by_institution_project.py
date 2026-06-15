# -*- coding: utf-8 -*-
"""
统计 data/output 下所有机构 JSON / JSONL 文件的数据情况，
并同时生成 JSON 统计文件和 Excel 统计文件。

本版本支持：不同机构、不同栏目分别设置不同的分组字段。

建议项目结构：

项目根目录/
├── scripts/
│   └── stats_by_institution_project.py
├── data/
│   ├── output/
│   │   ├── 中国疾病预防控制中心.json
│   │   ├── 国家疾病预防控制局.json
│   │   └── ...
│   └── statistic/
│       ├── all_institution_stats.json
│       └── all_institution_stats.xlsx

直接运行：

    python scripts/stats_by_institution_project.py

默认会读取：

    data/output/

默认会输出：

    data/statistic/all_institution_stats.json
    data/statistic/all_institution_stats.xlsx

如果没有安装 openpyxl，先运行：

    python -m pip install openpyxl

如果只想生成 JSON，不生成 Excel：

    python scripts/stats_by_institution_project.py --no-excel

如果临时想让所有机构、所有栏目都按同一字段分组，可以用命令行覆盖：

    python scripts/stats_by_institution_project.py --group-fields policy_category

或多层分组：

    python scripts/stats_by_institution_project.py --group-fields source_department document_type policy_category
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union


# ============================================================
# 路径配置
# ============================================================

# 当前脚本在 scripts/ 目录下，所以 parents[1] 就是项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 默认读取目录：项目根目录/data/output
DEFAULT_INPUT_DIR = PROJECT_ROOT / "great1" / "output"

# 默认输出目录：项目根目录/data/statistic
DEFAULT_STATISTIC_DIR = PROJECT_ROOT / "great1" / "logs" / "statistic"

# 默认输出 JSON 文件
DEFAULT_OUTPUT_FILE = DEFAULT_STATISTIC_DIR / "great1_institution_stats.json"

# 默认输出 Excel 文件
DEFAULT_EXCEL_OUTPUT_FILE = DEFAULT_STATISTIC_DIR / "great1_institution_stats.xlsx"


# ============================================================
# 统计字段配置：后面主要改这里
# ============================================================

# 机构名称字段。
# 你的数据里通常可能是：
# source.site_name
# 或者直接是 site_name
INSTITUTION_FIELD = "site_name"

# 栏目字段。
# 你的数据里通常可能是：
# source.channel_name
# 或者直接是 channel_name
CHANNEL_FIELD = "channel_name"

# 附件字段。
# 通常是 attachments。
ATTACHMENT_FIELD = "attachments"

# 字段缺失、为空时，放进这个类别。
MISSING_VALUE = "未填写"

# 如果某个分类字段是列表，如何处理：
# "join"：合并成一个分类，例如 ["A", "B"] -> "A、B"
# "explode"：一篇文章同时计入多个分类，例如 ["A", "B"] -> A 和 B 都 +1
LIST_VALUE_MODE = "join"


# ============================================================
# 分机构 / 分栏目配置分组字段
# ============================================================
#
# 这里是本版本最重要的配置。
#
# 写法 1：某个机构的所有栏目都按同一字段分组
#
# "中国疾病预防控制中心": ["policy_category"]
#
# 写法 2：某个机构的不同栏目分别设置
#
# "国家卫生健康委员会": {
#     "__default__": [],
#     "规范性文件": ["policy_category"],
#     "政策法规": ["policy_category"],
# }
#
# 说明：
# 1. "__default__" 表示这个机构其他栏目默认怎么分组。
# 2. [] 表示不继续细分。
# 3. ["policy_category"] 表示按 policy_category 分一层。
# 4. ["source_department", "policy_category"] 表示先按 source_department，再按 policy_category 分两层。
#
# 你现在的需求：
# - 卫健委：只给“规范性文件”和“政策法规”加新分类
# - 疾控局：只给“疾病预防控制标准”加新分类
# - 其他机构：默认按 policy_category 分
#
INSTITUTION_GROUP_CONFIG: Dict[str, Union[List[str], Dict[str, List[str]]]] = {
    "中国疾病预防控制中心": [],

    "中国疾控中心慢病中心": [],

    "国家疾病预防控制局": {
        "__default__": [],
        "疾病预防控制标准": ["policy_category"],
    },

    "国家卫生健康委员会": {
        "__default__": [],
        "规范性文件": ["policy_category"],
        "政策法规": ["policy_category"],
    },
}

# 如果某个机构不在 INSTITUTION_GROUP_CONFIG 里面，就使用这里的默认分组。
# [] 表示未知机构不细分。
DEFAULT_GROUP_FIELDS_FOR_UNKNOWN_INSTITUTION: List[str] = []


def get_group_fields_for_channel(
    institution_name: str,
    channel_name: str,
    cli_group_fields: Optional[List[str]] = None,
) -> List[str]:
    """
    根据机构名和栏目名，决定当前栏目要按哪些字段继续分组。

    优先级：
    1. 命令行 --group-fields，如果用户传了，就所有机构、所有栏目统一使用它。
    2. INSTITUTION_GROUP_CONFIG 里的配置。
    3. DEFAULT_GROUP_FIELDS_FOR_UNKNOWN_INSTITUTION。
    """
    if cli_group_fields is not None:
        return cli_group_fields

    config = INSTITUTION_GROUP_CONFIG.get(institution_name)

    if config is None:
        return DEFAULT_GROUP_FIELDS_FOR_UNKNOWN_INSTITUTION

    if isinstance(config, list):
        return config

    if isinstance(config, dict):
        if channel_name in config:
            return config[channel_name]

        return config.get("__default__", [])

    return []


# ============================================================
# 基础工具函数
# ============================================================

def empty_stats() -> Dict[str, Any]:
    """每个统计节点都包含这三个基础字段。"""
    return {
        "总数据量": 0,
        "总附件数": 0,
        "没有附件的文章数": 0,
    }


_SENTINEL = object()


def recursive_find(obj: Any, key: str) -> Any:
    """
    递归查找字段。

    这样你只写 source_department，
    即使实际路径是 organization.source_department，
    脚本也能找到。
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]

        for value in obj.values():
            found = recursive_find(value, key)
            if found is not _SENTINEL:
                return found

    elif isinstance(obj, list):
        for item in obj:
            found = recursive_find(item, key)
            if found is not _SENTINEL:
                return found

    return _SENTINEL


def get_value(record: Dict[str, Any], field_path: str, default: Any = None) -> Any:
    """
    获取字段值，支持两种写法：

    1. 完整路径：
       source.channel_name
       organization.source_department
       classification.policy_category

    2. 简写字段名：
       channel_name
       source_department
       policy_category

    如果完整路径找不到，会自动递归查找最后一级字段名。
    """
    if not field_path:
        return default

    # 先按完整路径查找
    current: Any = record
    path_found = True

    for part in field_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            path_found = False
            break

    if path_found:
        return current

    # 完整路径找不到，再递归查找最后一级字段名
    last_key = field_path.split(".")[-1]
    found = recursive_find(record, last_key)

    if found is not _SENTINEL:
        return found

    return default


def normalize_values(value: Any) -> List[str]:
    """把任意字段值统一转成分类名列表。"""
    if value is None:
        return [MISSING_VALUE]

    if isinstance(value, str):
        value = value.strip()
        return [value if value else MISSING_VALUE]

    if isinstance(value, list):
        cleaned = [str(v).strip() for v in value if v is not None and str(v).strip()]

        if not cleaned:
            return [MISSING_VALUE]

        if LIST_VALUE_MODE == "explode":
            return cleaned

        return ["、".join(cleaned)]

    if isinstance(value, dict):
        if not value:
            return [MISSING_VALUE]
        return [json.dumps(value, ensure_ascii=False, sort_keys=True)]

    text = str(value).strip()
    return [text if text else MISSING_VALUE]


def get_attachment_count(record: Dict[str, Any]) -> int:
    """
    统计一篇文章的附件数量。

    支持以下情况：
    1. attachments 是列表：按列表长度统计
    2. attachments 是字典：非空字典算 1 个附件
    3. attachments 是整数：直接作为附件数量
    4. attachments 是字符串：
       - 空字符串算 0
       - JSON 字符串会尝试解析
       - 普通非空字符串算 1
    """
    attachments = get_value(record, ATTACHMENT_FIELD, [])

    if attachments is None:
        return 0

    if isinstance(attachments, list):
        return len(attachments)

    if isinstance(attachments, dict):
        return 1 if attachments else 0

    if isinstance(attachments, int):
        return max(attachments, 0)

    if isinstance(attachments, str):
        text = attachments.strip()

        if not text:
            return 0

        try:
            parsed = json.loads(text)

            if isinstance(parsed, list):
                return len(parsed)

            if isinstance(parsed, dict):
                return 1 if parsed else 0

            if isinstance(parsed, int):
                return max(parsed, 0)

        except json.JSONDecodeError:
            pass

        return 1

    return 0


def update_stats(node: Dict[str, Any], record: Dict[str, Any]) -> None:
    """更新当前统计节点的数量。"""
    attachment_count = get_attachment_count(record)

    node["总数据量"] += 1
    node["总附件数"] += attachment_count

    if attachment_count == 0:
        node["没有附件的文章数"] += 1


# ============================================================
# 分层统计逻辑
# ============================================================

def add_group_stats(
    node: Dict[str, Any],
    record: Dict[str, Any],
    group_fields: List[str],
    level: int = 0,
) -> None:
    """
    在栏目下面按 group_fields 递归分层统计。

    例如：

    group_fields = ["source_department", "policy_category"]

    输出结构会变成：

    栏目名
    └── source_department
        └── 综合司
            ├── 总数据量
            ├── 总附件数
            ├── 没有附件的文章数
            └── policy_category
                └── 疾病防控
                    ├── 总数据量
                    ├── 总附件数
                    └── 没有附件的文章数
    """
    if level >= len(group_fields):
        return

    field = group_fields[level]
    field_value = get_value(record, field, MISSING_VALUE)
    category_values = normalize_values(field_value)

    if field not in node:
        node[field] = {}

    for category in category_values:
        child = node[field].setdefault(category, empty_stats())
        update_stats(child, record)
        add_group_stats(child, record, group_fields, level + 1)


def add_record_to_result(
    result: Dict[str, Any],
    record: Dict[str, Any],
    fallback_institution_name: str,
    cli_group_fields: Optional[List[str]] = None,
) -> None:
    """
    把一篇文章加入最终统计结果。
    """
    institution_values = normalize_values(
        get_value(record, INSTITUTION_FIELD, fallback_institution_name)
    )
    institution_name = institution_values[0] if institution_values else fallback_institution_name

    institution_node = result.setdefault(institution_name, empty_stats())

    if "栏目" not in institution_node:
        institution_node["栏目"] = {}

    update_stats(institution_node, record)

    channel_values = normalize_values(get_value(record, CHANNEL_FIELD, MISSING_VALUE))

    for channel_name in channel_values:
        channel_node = institution_node["栏目"].setdefault(channel_name, empty_stats())
        update_stats(channel_node, record)

        group_fields = get_group_fields_for_channel(
            institution_name=institution_name,
            channel_name=channel_name,
            cli_group_fields=cli_group_fields,
        )

        if group_fields:
            add_group_stats(channel_node, record, group_fields)


# ============================================================
# 文件读取逻辑
# ============================================================

def read_records_from_json_file(file_path: Path) -> Iterable[Dict[str, Any]]:
    """
    读取 .json 文件。

    支持以下结构：

    1. JSON 数组：
       [
         {...},
         {...}
       ]

    2. JSON 对象中包含列表字段：
       {
         "records": [{...}, {...}]
       }

       支持的列表字段名：
       records / items / data / documents / articles / results

    3. 单个 JSON 对象：
       {...}
    """
    with file_path.open("r", encoding="utf-8") as f:
        obj = json.load(f)

    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                yield item
        return

    if isinstance(obj, dict):
        candidate_keys = ["records", "items", "data", "documents", "articles", "results"]

        for key in candidate_keys:
            value = obj.get(key)

            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item
                return

        # 如果它本身就是单篇文章对象
        yield obj


def read_records_from_jsonl_file(file_path: Path) -> Iterable[Dict[str, Any]]:
    """
    读取 .jsonl 文件，一行一篇文章。
    """
    with file_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[跳过] {file_path} 第 {line_no} 行不是合法 JSON：{e}")
                continue

            if isinstance(obj, dict):
                yield obj


def read_records(file_path: Path) -> Iterable[Dict[str, Any]]:
    suffix = file_path.suffix.lower()

    if suffix == ".json":
        yield from read_records_from_json_file(file_path)

    elif suffix == ".jsonl":
        yield from read_records_from_jsonl_file(file_path)


def iter_input_files(
    input_path: Path,
    skip_paths: Optional[List[Path]] = None,
) -> Iterable[Path]:
    """
    输入可以是单个文件，也可以是目录。

    目录模式下会递归读取所有 .json / .jsonl 文件。
    """
    allowed_suffixes = {".json", ".jsonl"}
    resolved_skip_paths = set()

    if skip_paths:
        for path in skip_paths:
            try:
                resolved_skip_paths.add(path.resolve())
            except FileNotFoundError:
                resolved_skip_paths.add(path.absolute())

    if input_path.is_file():
        if input_path.suffix.lower() in allowed_suffixes:
            yield input_path
        return

    if input_path.is_dir():
        for file_path in sorted(input_path.rglob("*")):
            if not file_path.is_file():
                continue

            if file_path.suffix.lower() not in allowed_suffixes:
                continue

            # 防止输出 JSON 文件刚好在输入目录下，下一次运行又被当成输入统计
            try:
                resolved_file_path = file_path.resolve()
            except FileNotFoundError:
                resolved_file_path = file_path.absolute()

            if resolved_file_path in resolved_skip_paths:
                continue

            yield file_path


# ============================================================
# 主统计流程
# ============================================================

def build_statistics(
    input_path: Path,
    output_json_path: Path,
    output_excel_path: Optional[Path],
    cli_group_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    构建所有机构的统计结果。
    """
    result: Dict[str, Any] = {}

    total_files = 0
    total_records = 0

    skip_paths = [output_json_path]
    if output_excel_path is not None:
        skip_paths.append(output_excel_path)

    for file_path in iter_input_files(input_path, skip_paths=skip_paths):
        total_files += 1
        file_records = 0

        # 如果文章里没有机构字段，就用文件名作为机构名兜底
        fallback_institution_name = file_path.stem

        for record in read_records(file_path):
            file_records += 1
            total_records += 1

            add_record_to_result(
                result=result,
                record=record,
                fallback_institution_name=fallback_institution_name,
                cli_group_fields=cli_group_fields,
            )

        try:
            display_path = file_path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_path = file_path

        print(f"[完成] {display_path}：读取 {file_records} 条数据")

    print(f"[汇总] 共处理 {total_files} 个文件，{total_records} 条数据")

    return result


# ============================================================
# Excel 输出逻辑
# ============================================================

def set_header_style(ws, header_row: int = 1) -> None:
    """设置表头样式。"""
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="D9E2F3")

    for cell in ws[header_row]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center
        cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)


def style_body(ws) -> None:
    """设置正文样式。"""
    from openpyxl.styles import Alignment, Border, Side

    thin = Side(style="thin", color="E5E7EB")
    border = Border(top=thin, bottom=thin, left=thin, right=thin)

    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = border


def autosize_columns(ws, max_width: int = 36) -> None:
    """自动调整列宽，但限制最大宽度，避免太宽。"""
    for column_cells in ws.columns:
        max_length = 0
        column_letter = column_cells[0].column_letter

        for cell in column_cells:
            value = cell.value
            if value is None:
                continue

            max_length = max(max_length, len(str(value)))

        adjusted_width = min(max(max_length + 2, 10), max_width)
        ws.column_dimensions[column_letter].width = adjusted_width


def add_table_if_has_data(ws, table_name: str) -> None:
    """给工作表添加 Excel 表格样式。"""
    from openpyxl.worksheet.table import Table, TableStyleInfo

    if ws.max_row < 2 or ws.max_column < 1:
        return

    ref = f"A1:{ws.cell(row=ws.max_row, column=ws.max_column).coordinate}"
    table = Table(displayName=table_name, ref=ref)
    style = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    table.tableStyleInfo = style
    ws.add_table(table)


def freeze_and_filter(ws) -> None:
    """冻结首行并开启筛选。"""
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{ws.cell(row=ws.max_row, column=ws.max_column).coordinate}"


def write_sheet(ws, headers: List[str], rows: List[List[Any]], table_name: str) -> None:
    """写入一个工作表。"""
    ws.append(headers)

    for row in rows:
        ws.append(row)

    set_header_style(ws)
    style_body(ws)
    freeze_and_filter(ws)
    autosize_columns(ws)
    add_table_if_has_data(ws, table_name)


def build_overview_rows(stats: Dict[str, Any]) -> List[List[Any]]:
    """生成机构总览表行。"""
    rows: List[List[Any]] = []

    total_data = 0
    total_attachments = 0
    total_no_attachment_articles = 0

    for institution_name, institution_node in stats.items():
        data_count = institution_node.get("总数据量", 0)
        attachment_count = institution_node.get("总附件数", 0)
        no_attachment_count = institution_node.get("没有附件的文章数", 0)

        rows.append([
            institution_name,
            data_count,
            attachment_count,
            no_attachment_count,
        ])

        total_data += data_count
        total_attachments += attachment_count
        total_no_attachment_articles += no_attachment_count

    rows.append([
        "总计",
        total_data,
        total_attachments,
        total_no_attachment_articles,
    ])

    return rows


def build_channel_rows(stats: Dict[str, Any]) -> List[List[Any]]:
    """生成栏目统计表行。"""
    rows: List[List[Any]] = []

    for institution_name, institution_node in stats.items():
        channels = institution_node.get("栏目", {})

        if not isinstance(channels, dict):
            continue

        for channel_name, channel_node in channels.items():
            rows.append([
                institution_name,
                channel_name,
                channel_node.get("总数据量", 0),
                channel_node.get("总附件数", 0),
                channel_node.get("没有附件的文章数", 0),
            ])

    return rows


def collect_group_rows_from_node(
    rows: List[List[Any]],
    institution_name: str,
    channel_name: str,
    node: Dict[str, Any],
    group_fields: List[str],
    level: int = 0,
    path_parts: Optional[List[str]] = None,
) -> None:
    """
    递归展开栏目下的分类统计。

    Excel 中会输出成扁平表，支持任意层级，例如：
    机构 | 栏目 | 层级 | 分类字段 | 分类路径 | 分类名称 | 数据条数 | 总附件数 | 没有附件的文章数
    """
    if path_parts is None:
        path_parts = []

    if level >= len(group_fields):
        return

    field = group_fields[level]
    group_dict = node.get(field, {})

    if not isinstance(group_dict, dict):
        return

    for category_name, category_node in group_dict.items():
        if not isinstance(category_node, dict):
            continue

        current_path_parts = path_parts + [str(category_name)]

        rows.append([
            institution_name,
            channel_name,
            level + 1,
            field,
            " > ".join(current_path_parts),
            category_name,
            category_node.get("总数据量", 0),
            category_node.get("总附件数", 0),
            category_node.get("没有附件的文章数", 0),
        ])

        collect_group_rows_from_node(
            rows=rows,
            institution_name=institution_name,
            channel_name=channel_name,
            node=category_node,
            group_fields=group_fields,
            level=level + 1,
            path_parts=current_path_parts,
        )


def build_group_rows(
    stats: Dict[str, Any],
    cli_group_fields: Optional[List[str]] = None,
) -> List[List[Any]]:
    """生成分类统计表行。"""
    rows: List[List[Any]] = []

    for institution_name, institution_node in stats.items():
        channels = institution_node.get("栏目", {})

        if not isinstance(channels, dict):
            continue

        for channel_name, channel_node in channels.items():
            if not isinstance(channel_node, dict):
                continue

            group_fields = get_group_fields_for_channel(
                institution_name=institution_name,
                channel_name=channel_name,
                cli_group_fields=cli_group_fields,
            )

            if not group_fields:
                continue

            collect_group_rows_from_node(
                rows=rows,
                institution_name=institution_name,
                channel_name=channel_name,
                node=channel_node,
                group_fields=group_fields,
            )

    return rows


def export_statistics_to_excel(
    stats: Dict[str, Any],
    output_excel_path: Path,
    cli_group_fields: Optional[List[str]] = None,
) -> None:
    """
    将统计结果导出为 Excel 文件。

    Excel 包含 3 个工作表：
    1. 机构总览
    2. 栏目统计
    3. 分类统计
    """
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise ImportError(
            "缺少 openpyxl，无法生成 Excel。请先运行：python -m pip install openpyxl"
        ) from e

    wb = Workbook()

    # 删除默认 Sheet，改成中文工作表名
    default_ws = wb.active
    wb.remove(default_ws)

    overview_ws = wb.create_sheet("机构总览")
    channel_ws = wb.create_sheet("栏目统计")
    group_ws = wb.create_sheet("分类统计")

    write_sheet(
        ws=overview_ws,
        headers=["数据来源", "数据条数", "总附件数", "没有附件的文章数"],
        rows=build_overview_rows(stats),
        table_name="OverviewTable",
    )

    write_sheet(
        ws=channel_ws,
        headers=["数据来源", "栏目", "数据条数", "总附件数", "没有附件的文章数"],
        rows=build_channel_rows(stats),
        table_name="ChannelTable",
    )

    write_sheet(
        ws=group_ws,
        headers=[
            "数据来源",
            "栏目",
            "层级",
            "分类字段",
            "分类路径",
            "分类名称",
            "数据条数",
            "总附件数",
            "没有附件的文章数",
        ],
        rows=build_group_rows(stats, cli_group_fields=cli_group_fields),
        table_name="GroupTable",
    )

    # 给数值列设置整数格式
    for ws in [overview_ws, channel_ws, group_ws]:
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, int):
                    cell.number_format = "0"

    output_excel_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_excel_path)


# ============================================================
# 命令行参数
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统计 data/output 下所有机构 JSON / JSONL 文件的数据情况，并同时生成 JSON 和 Excel"
    )

    parser.add_argument(
        "--input",
        "-i",
        default=str(DEFAULT_INPUT_DIR),
        help=f"输入文件或目录，默认：{DEFAULT_INPUT_DIR}",
    )

    parser.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_OUTPUT_FILE),
        help=f"输出统计 JSON 文件，默认：{DEFAULT_OUTPUT_FILE}",
    )

    parser.add_argument(
        "--excel-output",
        default=str(DEFAULT_EXCEL_OUTPUT_FILE),
        help=f"输出统计 Excel 文件，默认：{DEFAULT_EXCEL_OUTPUT_FILE}",
    )

    parser.add_argument(
        "--no-excel",
        action="store_true",
        help="只生成 JSON，不生成 Excel",
    )

    parser.add_argument(
        "--group-fields",
        nargs="+",
        default=None,
        help=(
            "临时覆盖所有机构、所有栏目的分组字段。"
            "例如：--group-fields source_department document_type policy_category。"
            "如果不传，就使用脚本里的 INSTITUTION_GROUP_CONFIG。"
        ),
    )

    return parser.parse_args()


def resolve_project_path(path_str: str) -> Path:
    """如果用户传相对路径，就按项目根目录解析。"""
    path = Path(path_str)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path


def main() -> None:
    args = parse_args()

    input_path = resolve_project_path(args.input)
    output_json_path = resolve_project_path(args.output)
    output_excel_path = None if args.no_excel else resolve_project_path(args.excel_output)

    if not input_path.exists():
        raise FileNotFoundError(f"输入路径不存在：{input_path}")

    result = build_statistics(
        input_path=input_path,
        output_json_path=output_json_path,
        output_excel_path=output_excel_path,
        cli_group_fields=args.group_fields,
    )

    output_json_path.parent.mkdir(parents=True, exist_ok=True)

    with output_json_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    try:
        display_json_path = output_json_path.relative_to(PROJECT_ROOT)
    except ValueError:
        display_json_path = output_json_path

    print(f"[输出] JSON 统计结果已保存到：{display_json_path}")

    if output_excel_path is not None:
        export_statistics_to_excel(
            stats=result,
            output_excel_path=output_excel_path,
            cli_group_fields=args.group_fields,
        )

        try:
            display_excel_path = output_excel_path.relative_to(PROJECT_ROOT)
        except ValueError:
            display_excel_path = output_excel_path

        print(f"[输出] Excel 统计结果已保存到：{display_excel_path}")


if __name__ == "__main__":
    main()
