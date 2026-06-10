import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "output"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
LOGS_DIR = DATA_DIR / "logs"

REPORT_PATH = LOGS_DIR / f"crawler_attachment_stats_{datetime.now():%Y%m%d_%H%M%S}.xlsx"

JSONL_ORG_MAP = {
    "nhc_all_documents.jsonl": "国家卫生健康委员会",
    "ndcpa_all_documents.jsonl": "国家疾病预防控制局",
    "chinacdc_all_documents.jsonl": "中国疾病预防控制中心",
    "ncncd_all_documents.jsonl": "中国疾控中心慢病中心",
}


def clean_sheet_name(name):
    name = re.sub(r"[\[\]\:\*\?\/\\]", "_", str(name))
    return name[:31] or "Sheet"


def cell_ref(row_idx, col_idx):
    letters = ""
    col = col_idx
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row_idx}"


def value_to_cell_xml(value, row_idx, col_idx):
    ref = cell_ref(row_idx, col_idx)
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = str(value)
    if len(text) > 32767:
        text = text[:32764] + "..."
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'


def worksheet_xml(rows):
    rows_xml = []
    for row_idx, row in enumerate(rows, 1):
        cells = "".join(value_to_cell_xml(value, row_idx, col_idx) for col_idx, value in enumerate(row, 1))
        rows_xml.append(f'<row r="{row_idx}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        '</worksheet>'
    )


def write_xlsx(path, sheets):
    sheet_items = [(clean_sheet_name(name), rows) for name, rows in sheets]
    workbook_sheets = []
    workbook_rels = []
    content_overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]

    for idx, (name, _) in enumerate(sheet_items, 1):
        workbook_sheets.append(f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    style_rel_id = len(sheet_items) + 1
    workbook_rels.append(
        f'<Relationship Id="rId{style_rel_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(workbook_sheets)}</sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(workbook_rels)}</Relationships>'
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{"".join(content_overrides)}</Types>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>'
    )

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/styles.xml", styles_xml)
        for idx, (_, rows) in enumerate(sheet_items, 1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", worksheet_xml(rows))


def read_jsonl(path):
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                yield {"_parse_error": f"{path.name}:{line_no}: {exc}", "attachments": []}


def as_list(value):
    return value if isinstance(value, list) else []


def get_nested(record, *keys, default=""):
    cur = record
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def collect_filesystem_attachment_counts():
    counts = defaultdict(int)
    org_totals = defaultdict(int)
    rows = [["机构", "栏目", "文章标题目录", "附件文件数", "文章目录路径"]]

    for org_dir in ATTACHMENTS_DIR.iterdir() if ATTACHMENTS_DIR.exists() else []:
        if not org_dir.is_dir():
            continue
        org = org_dir.name
        for channel_dir in org_dir.iterdir():
            if not channel_dir.is_dir():
                continue
            channel = channel_dir.name
            direct_files = [p for p in channel_dir.iterdir() if p.is_file()]
            if direct_files:
                counts[(org, channel)] += len(direct_files)
                org_totals[org] += len(direct_files)
                rows.append([org, channel, "(栏目目录下文件)", len(direct_files), str(channel_dir.relative_to(BASE_DIR))])
            for article_dir in channel_dir.iterdir():
                if not article_dir.is_dir():
                    continue
                file_count = sum(1 for p in article_dir.rglob("*") if p.is_file())
                counts[(org, channel)] += file_count
                org_totals[org] += file_count
                rows.append([org, channel, article_dir.name, file_count, str(article_dir)])

    loose_files = [p for p in ATTACHMENTS_DIR.iterdir() if p.is_file()] if ATTACHMENTS_DIR.exists() else []
    if loose_files:
        rows.append(["(未归入三机构目录)", "(根目录)", "(根目录散落文件)", len(loose_files), str(ATTACHMENTS_DIR)])

    return counts, org_totals, rows


def main():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fs_channel_counts, fs_org_totals, fs_rows = collect_filesystem_attachment_counts()

    article_rows = [[
        "机构",
        "栏目",
        "发布日期",
        "标题",
        "URL",
        "JSON附件数",
        "成功下载附件数",
        "是否有附件",
        "附件名称",
        "附件本地路径",
        "来源文件",
        "doc_id",
    ]]
    missing_rows = [["机构", "栏目", "发布日期", "标题", "URL", "来源文件", "doc_id"]]
    channel_stats = defaultdict(lambda: {"docs": 0, "with": 0, "without": 0, "json_attach": 0, "downloaded": 0})
    org_stats = defaultdict(lambda: {"docs": 0, "with": 0, "without": 0, "json_attach": 0, "downloaded": 0})

    parse_errors = []

    for jsonl_path in sorted(OUTPUT_DIR.glob("*_all_documents.jsonl")):
        org = JSONL_ORG_MAP.get(jsonl_path.name, jsonl_path.stem.replace("_all_documents", ""))
        for record in read_jsonl(jsonl_path):
            if "_parse_error" in record:
                parse_errors.append(record["_parse_error"])
                continue
            source = get_nested(record, "source", default={})
            dates = get_nested(record, "dates", default={})
            channel = source.get("channel_name", "") if isinstance(source, dict) else ""
            title = record.get("title") or get_nested(record, "raw", "raw_title", default="")
            publish_date = dates.get("publish_date", "") if isinstance(dates, dict) else ""
            url = record.get("url", "")
            attachments = as_list(record.get("attachments"))
            json_attach_count = len(attachments)
            downloaded_count = sum(1 for att in attachments if isinstance(att, dict) and att.get("download_status") == "success")
            has_attachment = "是" if max(json_attach_count, downloaded_count) > 0 else "否"
            attachment_names = "; ".join(str(att.get("name", "")) for att in attachments if isinstance(att, dict))
            # attachment_paths = "; ".join(str(att.get("local_path", "")) for att in attachments if isinstance(att, dict))
            _rel_paths = []
            for att in attachments:
                if isinstance(att, dict) and att.get("local_path"):
                    try:
                        _rel_paths.append(str(Path(att["local_path"]).relative_to(BASE_DIR)))
                    except ValueError:
                        _rel_paths.append(str(att["local_path"]))
            attachment_paths = "; ".join(_rel_paths)

            stat = channel_stats[(org, channel)]
            stat["docs"] += 1
            stat["with"] += 1 if has_attachment == "是" else 0
            stat["without"] += 1 if has_attachment == "否" else 0
            stat["json_attach"] += json_attach_count
            stat["downloaded"] += downloaded_count

            org_stat = org_stats[org]
            org_stat["docs"] += 1
            org_stat["with"] += 1 if has_attachment == "是" else 0
            org_stat["without"] += 1 if has_attachment == "否" else 0
            org_stat["json_attach"] += json_attach_count
            org_stat["downloaded"] += downloaded_count

            article_rows.append([
                org,
                channel,
                publish_date,
                title,
                url,
                json_attach_count,
                downloaded_count,
                has_attachment,
                attachment_names,
                attachment_paths,
                jsonl_path.name,
                record.get("doc_id", ""),
            ])
            if has_attachment == "否":
                missing_rows.append([org, channel, publish_date, title, url, jsonl_path.name, record.get("doc_id", "")])

    org_rows = [["机构", "数据条数", "有附件文章数", "无附件文章数", "JSON附件数", "成功下载附件数", "附件目录文件数"]]
    for org, stat in sorted(org_stats.items()):
        org_rows.append([
            org,
            stat["docs"],
            stat["with"],
            stat["without"],
            stat["json_attach"],
            stat["downloaded"],
            fs_org_totals.get(org, 0),
        ])

    channel_rows = [[
        "机构",
        "栏目",
        "数据条数",
        "有附件文章数",
        "无附件文章数",
        "JSON附件数",
        "成功下载附件数",
        "附件目录文件数",
    ]]
    for (org, channel), stat in sorted(channel_stats.items()):
        channel_rows.append([
            org,
            channel,
            stat["docs"],
            stat["with"],
            stat["without"],
            stat["json_attach"],
            stat["downloaded"],
            fs_channel_counts.get((org, channel), 0),
        ])

    note_rows = [
        ["说明项", "内容"],
        ["统计时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        # ["JSON目录", str(OUTPUT_DIR)],
        # ["附件目录", str(ATTACHMENTS_DIR)],
        ["JSON目录", str(OUTPUT_DIR.relative_to(BASE_DIR))],
        ["附件目录", str(ATTACHMENTS_DIR.relative_to(BASE_DIR))],
        ["无附件判定", "JSON附件数和成功下载附件数都为0"],
        ["附件目录文件数", "按 data/attachments/机构/栏目/文章目录 下实际文件数汇总，仅作为本地文件核对"],
        ["解析错误数", len(parse_errors)],
    ]
    note_rows.extend([["解析错误", err] for err in parse_errors])

    write_xlsx(
        REPORT_PATH,
        [
            ("说明", note_rows),
            ("机构汇总", org_rows),
            ("栏目汇总", channel_rows),
            ("文章明细", article_rows),
            ("无附件文章", missing_rows),
            ("附件目录核对", fs_rows),
        ],
    )
    # print(REPORT_PATH)
    json_report_data = {}
    
    # 首先初始化所有的机构总数据条数
    for org, stat in org_stats.items():
        json_report_data[org] = {
            "总数据条数": stat["docs"],
            "栏目": {}
        }
        
    # 填充各个栏目的数据
    for (org, channel), stat in channel_stats.items():
        # 防御性判断：如果没有栏目名字或者机构没被记录的话
        if org not in json_report_data:
            json_report_data[org] = {"总数据条数": 0, "栏目": {}}
            
        json_report_data[org]["栏目"][channel] = {
            "数据条数": stat["docs"],
            "总附件数": stat["json_attach"],  # 以 JSON 中解析到的附件数为准
            "没有附件的文章数": stat["without"]
        }
        
    # 将 JSON 写出到同级目录下
    json_report_path = LOGS_DIR / f"crawler_attachment_stats_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(json_report_path, "w", encoding="utf-8") as f:
        json.dump(json_report_data, f, ensure_ascii=False, indent=2)
    # ----------------------------------------------------------------

    print(f"Excel Report: {REPORT_PATH}")
    print(f"JSON Report:  {json_report_path}")


if __name__ == "__main__":
    main()
