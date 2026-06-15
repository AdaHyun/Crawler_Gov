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

NHC_ORG = "国家卫生健康委员会"
NDCPA_ORG = "国家疾病预防控制局"
NDCPA_STANDARD_CHANNEL = "疾病预防控制标准"
NHC_CLASSIFIED_CHANNELS = {"规范性文件", "政策解读", "政策法规"}

UNCONFIRMED_CATEGORY = "待确认"
NO_SECOND_LEVEL = ""

NDCPA_STANDARD_LEVEL1_KEYWORDS = [
    ("传染病", ["结核", "基孔肯雅", "软下疳", "新冠", "冠状病毒", "流感", "艾滋", "梅毒", "麻风", "手足口", "登革", "布鲁氏", "霍乱", "鼠疫", "炭疽", "狂犬", "肝炎", "传染病"]),
    ("寄生虫病", ["疟原虫", "疟疾", "寄生虫", "钩虫", "利什曼", "血吸虫", "包虫", "弓形虫", "绦虫", "蛔虫"]),
    ("地方病", ["碘", "氟", "砷", "地方病", "克山病", "大骨节", "地氟", "碘缺乏"]),
    ("环境健康", ["环境健康", "空气污染", "饮用水", "公共场所", "室内空气", "环境卫生", "土壤", "噪声"]),
    ("学校卫生", ["学校卫生", "学生", "儿童青少年", "近视", "课桌椅", "托幼机构", "校园"]),
    ("消毒", ["消毒", "灭菌", "消毒剂", "消毒产品", "医院消毒", "疫源地"]),
    ("疾病预防控制信息", ["疾病预防控制信息", "信息", "编码", "数据集", "监测信息", "报告卡", "个案"]),
    ("伤害预防控制", ["伤害", "中毒", "跌倒", "溺水", "道路交通", "创伤"]),
    ("其他类", ["其他"]),
]

NHC_TOPIC_KEYWORDS = [
    ("医疗服务与医院管理", ["医疗质量", "医院", "护理", "康复", "临床", "医疗服务", "医政", "医疗机构", "诊疗", "急诊", "质控"]),
    ("基层卫生", ["县域", "基层", "家庭医生", "社区卫生", "乡镇卫生院", "村卫生室", "全科医生"]),
    ("公共卫生与传染病防控", ["公共卫生", "传染病", "防控", "疫情", "预防接种", "疫苗", "结核", "艾滋", "突发公共卫生"]),
    ("职业健康", ["职业病", "防暑降温", "用人单位职业健康", "职业健康", "尘肺", "放射卫生"]),
    ("妇幼健康与托育", ["妇幼", "母婴", "托育", "婴幼儿", "出生缺陷", "儿童健康", "孕产妇"]),
    ("老龄健康与医养结合", ["老龄", "医养结合", "老年", "安宁疗护"]),
    ("中医药", ["中医", "中医药", "民族医"]),
    ("药品耗材与行业纠风", ["药品", "耗材", "行业纠风", "医药购销", "回扣", "商业贿赂", "廉洁从业"]),
    ("卫生标准与食品安全", ["卫生标准", "食品安全", "食品", "营养", "标准"]),
    ("人才培训与科研", ["人才", "培训", "科研", "住院医师", "继续医学教育", "实验室"]),
    ("信息化与数据", ["信息化", "数据", "互联网", "电子病历", "信息平台", "网络安全"]),
    ("宣传科普", ["宣传", "科普", "健康教育", "健康促进"]),
    ("统计公报", ["统计公报", "公报", "统计"]),
    ("其他", ["其他"]),
]


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


def normalize_standard_code(value):
    text = str(value or "").upper().strip()
    text = re.sub(r"[\s\-/]+", "_", text)
    if text in {"WS_T", "WST"}:
        return "WS_T"
    if text in {"GB_T", "GBT"}:
        return "GB_T"
    if text in {"WS", "GB"}:
        return text
    return text


def extract_standard_code(title):
    text = str(title or "").upper()
    match = re.search(r"(?<![A-Z0-9])(WS\s*[_/]?\s*T|WST|GB\s*[_/]?\s*T|GBT|WS|GB)(?![A-Z0-9])", text)
    return normalize_standard_code(match.group(1)) if match else UNCONFIRMED_CATEGORY


def match_keyword_category(title, rules, fallback=UNCONFIRMED_CATEGORY):
    text = str(title or "")
    for category, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return category
    return fallback


def attachment_relative_parts(local_path):
    if not local_path:
        return []
    try:
        path = Path(local_path)
        if not path.is_absolute():
            path = BASE_DIR / path
        rel = path.relative_to(ATTACHMENTS_DIR)
    except (ValueError, OSError):
        return []
    return list(rel.parts)


def first_attachment_category_parts(attachments, org, channel):
    for att in attachments:
        if not isinstance(att, dict):
            continue
        parts = attachment_relative_parts(att.get("local_path"))
        if len(parts) >= 3 and parts[0] == org and parts[1] == channel:
            return parts[2:]
    return []


def classify_record(org, channel, title, attachments):
    parts = first_attachment_category_parts(attachments, org, channel)
    if org == NDCPA_ORG and channel == NDCPA_STANDARD_CHANNEL:
        level1 = parts[0] if len(parts) >= 1 else match_keyword_category(title, NDCPA_STANDARD_LEVEL1_KEYWORDS)
        level2 = normalize_standard_code(parts[1]) if len(parts) >= 2 else extract_standard_code(title)
        return level1 or UNCONFIRMED_CATEGORY, level2 or UNCONFIRMED_CATEGORY

    if org == NHC_ORG and channel in NHC_CLASSIFIED_CHANNELS:
        level1 = parts[0] if len(parts) >= 1 else match_keyword_category(title, NHC_TOPIC_KEYWORDS, fallback="其他")
        return level1 or "其他", NO_SECOND_LEVEL

    return None


def add_stats(bucket, json_attach_count, downloaded_count, has_attachment):
    bucket["docs"] += 1
    bucket["with"] += 1 if has_attachment == "是" else 0
    bucket["without"] += 1 if has_attachment == "否" else 0
    bucket["json_attach"] += json_attach_count
    bucket["downloaded"] += downloaded_count


def new_stat_bucket():
    return {"docs": 0, "with": 0, "without": 0, "json_attach": 0, "downloaded": 0}


def collect_filesystem_attachment_counts():
    counts = defaultdict(int)
    org_totals = defaultdict(int)
    category_counts = defaultdict(int)
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

            if org == NDCPA_ORG and channel == NDCPA_STANDARD_CHANNEL:
                for level1_dir in channel_dir.iterdir():
                    if not level1_dir.is_dir():
                        continue
                    for level2_dir in level1_dir.iterdir():
                        if not level2_dir.is_dir():
                            continue
                        level2 = normalize_standard_code(level2_dir.name)
                        file_count = sum(1 for p in level2_dir.rglob("*") if p.is_file())
                        counts[(org, channel)] += file_count
                        org_totals[org] += file_count
                        category_counts[(org, channel, level1_dir.name, level2)] += file_count
                        rows.append([
                            org,
                            channel,
                            f"{level1_dir.name}/{level2_dir.name}",
                            file_count,
                            str(level2_dir.relative_to(BASE_DIR)),
                        ])
                continue

            if org == NHC_ORG and channel in NHC_CLASSIFIED_CHANNELS:
                for level1_dir in channel_dir.iterdir():
                    if not level1_dir.is_dir():
                        continue
                    file_count = sum(1 for p in level1_dir.rglob("*") if p.is_file())
                    counts[(org, channel)] += file_count
                    org_totals[org] += file_count
                    category_counts[(org, channel, level1_dir.name, NO_SECOND_LEVEL)] += file_count
                    rows.append([org, channel, level1_dir.name, file_count, str(level1_dir.relative_to(BASE_DIR))])
                continue

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

    return counts, org_totals, category_counts, rows


def main():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fs_channel_counts, fs_org_totals, fs_category_counts, fs_rows = collect_filesystem_attachment_counts()

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
    classified_stats = defaultdict(new_stat_bucket)

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
            add_stats(stat, json_attach_count, downloaded_count, has_attachment)

            org_stat = org_stats[org]
            add_stats(org_stat, json_attach_count, downloaded_count, has_attachment)

            classification = classify_record(org, channel, title, attachments)
            if classification:
                level1, level2 = classification
                add_stats(classified_stats[(org, channel, level1, level2)], json_attach_count, downloaded_count, has_attachment)

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

    classified_rows = [[
        "机构",
        "栏目",
        "一级分类",
        "二级分类",
        "数据条数",
        "有附件文章数",
        "无附件文章数",
        "JSON附件数",
        "成功下载附件数",
        "附件目录文件数",
    ]]
    for (org, channel, level1, level2), stat in sorted(classified_stats.items()):
        classified_rows.append([
            org,
            channel,
            level1,
            level2,
            stat["docs"],
            stat["with"],
            stat["without"],
            stat["json_attach"],
            stat["downloaded"],
            fs_category_counts.get((org, channel, level1, level2), 0),
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
        ["新增分类统计", "对国家卫生健康委员会的规范性文件/政策解读/政策法规，以及国家疾病预防控制局的疾病预防控制标准增加分类统计"],
        ["分类来源优先级", "优先读取附件本地路径中的分类目录；没有路径时按标题关键词和标准编码规则推断"],
        ["待确认", "疾控标准中无法由路径、标题关键词或编码识别的分类会记为待确认"],
        ["解析错误数", len(parse_errors)],
    ]
    note_rows.extend([["解析错误", err] for err in parse_errors])

    write_xlsx(
        REPORT_PATH,
        [
            ("说明", note_rows),
            ("机构汇总", org_rows),
            ("栏目汇总", channel_rows),
            ("新分类统计", classified_rows),
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

    for (org, channel, level1, level2), stat in classified_stats.items():
        channel_data = json_report_data.setdefault(org, {"总数据条数": 0, "栏目": {}})["栏目"].setdefault(
            channel,
            {"数据条数": 0, "总附件数": 0, "没有附件的文章数": 0},
        )
        categories = channel_data.setdefault("分类", {})
        level1_data = categories.setdefault(
            level1,
            {"数据条数": 0, "总附件数": 0, "没有附件的文章数": 0},
        )
        level1_data["数据条数"] += stat["docs"]
        level1_data["总附件数"] += stat["json_attach"]
        level1_data["没有附件的文章数"] += stat["without"]
        if level2:
            level2_categories = level1_data.setdefault("二级分类", {})
            level2_categories[level2] = {
                "数据条数": stat["docs"],
                "总附件数": stat["json_attach"],
                "没有附件的文章数": stat["without"],
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
