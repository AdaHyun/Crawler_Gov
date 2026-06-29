import json
import pandas as pd
from pathlib import Path
import re

# --- 路径配置 ---
# __file__ 指向 scripts/export_to_excel.py，向上两级到达项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
JSON_DIR = BASE_DIR / "data" / "output"
EXCEL_OUTPUT_PATH = JSON_DIR / "DataSummary_test.xlsx"


def safe_sheet_name(name: str) -> str:
    """清理并截断 Sheet 名称，适应 Excel 最大 31 字符且无特殊符号的要求"""
    safe_name = re.sub(r'[\\/*?:\[\]]', '_', name)
    return safe_name[:31]


def flatten_item(item: dict) -> dict:
    """提取嵌套的 JSON 字典，将其压平为一维字典供 Excel 生成列名"""
    
    def get_nested(d: dict, *keys, default=""):
        """安全地获取多层嵌套字段"""
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k, default)
            else:
                return default
        return d if d is not None else default

    # 处理附件列表 (将多个附件名称及状态拼接到一个单元格中，用换行符隔开)
    attachments = item.get("attachments", [])
    att_info = []
    for att in attachments:
        if isinstance(att, dict):
            status = att.get('download_status', 'unknown')
            name = att.get('name', '未命名附件')
            att_info.append(f"[{status}] {name}")
    
    # 处理主题标签
    topic_tags = get_nested(item, "classification", "topic_tags", default=[])
    if not isinstance(topic_tags, list):
        topic_tags = []

    # 提取并压平我们需要展示的字段
    flat_data = {
        "文章编号": item.get("doc_id", ""),
        "标题": item.get("title", ""),
        "发布日期": get_nested(item, "dates", "publish_date"),
        "栏目名称": get_nested(item, "source", "channel_name"),
        "来源部门": get_nested(item, "organization", "source_department"),
        "文档类型": get_nested(item, "classification", "document_type"),
        "政策分类": get_nested(item, "classification", "policy_category"),
        "主题标签": " | ".join(topic_tags),
        "适用地区": get_nested(item, "classification", "target_region"),
        "附件信息": "\n".join(att_info),
        "图片数量": len(item.get("images", [])),
        "原文链接": item.get("url", ""),
        "爬取状态": get_nested(item, "crawl", "crawl_status"),
        "本地HTML路径": get_nested(item, "crawl", "raw_html_path"),
        "抓取日期": get_nested(item, "dates", "crawl_date"),
        "来源机构": get_nested(item, "source", "site_name"),
        # 正文截断前2000字符，防止内容过多撑爆Excel内存
        "正文预览": str(get_nested(item, "content", "body_text", default=""))[:2000] 
    }
    
    return flat_data


def main():
    if not JSON_DIR.exists():
        print(f"找不到数据目录: {JSON_DIR}")
        return

    json_files = list(JSON_DIR.glob("*.jsonl"))
    if not json_files:
        print("未找到任何 .jsonl 数据文件。")
        return

    print(f"发现 {len(json_files)} 个 JSONL 文件，正在构建 Excel...")

    # 使用 pandas 的 ExcelWriter，它可以支持在同一个文件里写入多个 Sheet
    with pd.ExcelWriter(EXCEL_OUTPUT_PATH, engine='openpyxl') as writer:
        for json_file in json_files:
            print(f"正在解析: {json_file.name}")
            
            data_list = []
            with open(json_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                        flat_item = flatten_item(item)
                        data_list.append(flat_item)
                    except json.JSONDecodeError:
                        continue
            
            if not data_list:
                print(f"  -> {json_file.name} 是空文件或无有效数据，已跳过。")
                continue

            # 转换为 DataFrame
            df = pd.DataFrame(data_list)
            
            # 使用文件名（去掉.jsonl后缀）作为 Sheet 的名称
            sheet_name = safe_sheet_name(json_file.stem)
            
            # 写入对应的 Sheet 中
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            print(f"  -> 成功抽取 {len(df)} 条记录至 Sheet: [{sheet_name}]")

    print(f"\n全部处理完成！Excel 文件已保存至:\n{EXCEL_OUTPUT_PATH.resolve()}")

if __name__ == "__main__":
    main()