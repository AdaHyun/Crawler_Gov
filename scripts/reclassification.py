import os
import json
import shutil
import re
import zipfile
from pathlib import Path

# --- 路径配置 ---
BASE_DIR = Path(__file__).resolve().parent.parent
JSON_DIR = BASE_DIR / "data" / "output"
TARGET_ROOT = BASE_DIR / "classified_export" 

def safe_path_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name or "未命名").strip()

def normalize_standard_code(title: str) -> str:
    match = re.search(r"^\s*(WS\s*[/_]?\s*T|GB\s*[/_]?\s*T|WS|GB)(?=[^a-zA-Z]|$)", title, re.IGNORECASE)
    if not match:
        return "未识别标准号"
    raw = re.sub(r"[\s/]+", "_", match.group(1).upper()).replace("__", "_")
    if raw in {"WST", "WS_T"}: return "WS_T"
    if raw in {"GBT", "GB_T"}: return "GB_T"
    return raw

def get_cdc_category(title: str) -> str:
    rules = {
        "传染病": ["冠状病毒", "结核", "基孔肯雅热", "软下疳", "疱疹", "传染病"],
        "寄生虫病": ["寄生虫", "疟原虫", "利什曼原虫", "钩虫"],
        "地方病": ["碘", "氟化物", "砷", "地方病"],
        "环境健康": ["饮用水", "热浪", "人体生物监测", "环境健康", "水处理"],
        "学校卫生": ["学生", "教室", "视力", "学校卫生", "中小学"],
        "消毒": ["消毒", "灭菌", "抑菌", "抗菌"],
        "疾病预防控制信息": ["信息接口", "数据集", "指标体系", "数据元", "数据底座", "信息平台"],
        "伤害预防控制": ["伤害"],
        "其他类": ["名词术语标准", "常用名词"]
    }
    for cat, keywords in rules.items():
        if any(k in title for k in keywords): return cat
    return "其他类"

def get_nhc_topic(title: str) -> str:
    rules = {
        "医疗服务与医院管理": ["医疗质量", "医院", "护理", "康复", "临床", "诊疗", "就医", "医疗机构", "医师"],
        "基层卫生": ["县域", "基层", "家庭医生", "社区卫生", "乡村医生", "乡镇卫生院"],
        "公共卫生与传染病防控": ["传染病", "疫情", "新冠", "疾控", "疫苗", "艾滋病", "结核", "公共卫生", "鼠疫"],
        "职业健康": ["职业病", "防暑降温", "用人单位", "职业健康", "职业禁忌", "粉尘"],
        "妇幼健康与托育": ["妇幼", "托育", "孕产妇", "儿童", "母婴", "出生缺陷", "新生儿", "优生"],
        "老龄健康与医养结合": ["老龄", "医养", "养老", "老年", "安宁疗护"],
        "中医药": ["中医", "中药", "针灸", "国医"],
        "药品耗材与行业纠风": ["药品", "耗材", "纠风", "医药购销", "药事", "基本药物", "行风"],
        "卫生标准与食品安全": ["卫生标准", "食品安全", "营养", "食品", "食源性", "三新食品"],
        "人才培训与科研": ["培训", "规培", "科研", "人才", "继续医学教育", "住院医师", "实验室"],
        "信息化与数据": ["信息化", "数据", "互联网+", "电子病历", "智慧医疗", "远程医疗", "统计"],
        "宣传科普": ["宣传", "科普", "健康教育", "健康促进"],
        "统计公报": ["公报", "调查制度", "年鉴"],
    }
    for topic, keywords in rules.items():
        if any(k in title for k in keywords): return topic
    return "其他"

def get_policy_law_category(title: str) -> str:
    if any(k in title for k in ["解读", "图解", "问答", "答问"]): return "解读"
    return "通知"

def resolve_actual_path(raw_local_path: str) -> Path | None:
    if not raw_local_path: return None
    # 完美契合 JSON 里形如 "data/attachments/..." 的路径
    return (BASE_DIR / raw_local_path).parent

def _guess_extension(file_path: Path) -> str:
    """X光抢救：通过读取文件的底层二进制头文件，精准推断真实格式"""
    try:
        with open(file_path, "rb") as f:
            data = f.read(10)
        
        if data.startswith(b"%PDF"): return ".pdf"
        if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"): return ".doc" # 旧版 word/excel (.doc/.xls)
        if data.startswith(b"\x89PNG\r\n\x1a\n"): return ".png"
        if data.startswith(b"\xff\xd8\xff"): return ".jpg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"): return ".gif"
        if data.startswith(b"Rar!\x1a\x07"): return ".rar"
        if data.startswith(b"7z\xbc\xaf\x27\x1c"): return ".7z"
        
        # docx, xlsx, pptx 本质上都是 zip 压缩包，需要扒开底层目录看里面的 xml 结构
        if data.startswith(b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(file_path, 'r') as z:
                    names = z.namelist()
                    if any(n.startswith('word/') for n in names): return ".docx"
                    if any(n.startswith('xl/') for n in names): return ".xlsx"
                    if any(n.startswith('ppt/') for n in names): return ".pptx"
            except Exception:
                pass
            return ".zip"
            
    except Exception:
        pass
    return ""

def smart_copy_tree(src_dir: Path, dst_dir: Path):
    """智能复制：在拷贝的过程中自动修复丢失的文件后缀"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.is_dir():
            smart_copy_tree(item, dst_dir / item.name)
        else:
            target_name = item.name
            
            # 如果文件完全没有后缀，或者是 ".unknown"，强行通过底层二进制推断它的真身
            if not item.suffix or "unknown" in item.suffix.lower():
                ext = _guess_extension(item)
                if ext:
                    # 抹除原有的名字里的 unknown 字符串再拼上真实的后缀
                    target_name = target_name.replace(".unknown", "").replace(".UNKNOWN", "")
                    # 如果原名里本来连 . 都没，直接加上推断出的后缀
                    target_name += ext
                    
            target_file = dst_dir / target_name
            if not target_file.exists():
                shutil.copy2(item, target_file)

def process_documents():
    if not JSON_DIR.exists():
        print(f"未找到 JSON 目录: {JSON_DIR}")
        return

    json_files = list(JSON_DIR.glob("*.jsonl"))
    if not json_files:
        print("未找到任何 .jsonl 数据文件")
        return

    success_count = 0
    target_channels = {"疾病预防控制标准", "规范性文件", "政策法规"}

    for json_file in json_files:
        print(f"\n>>> 正在处理文件: {json_file.name}")
        with open(json_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip(): continue

                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                channel_name = item.get("source", {}).get("channel_name", "").strip()
                title = item.get("title", "未知标题")
                
                # 拦截器 1：栏目名不匹配
                if channel_name not in target_channels:
                    continue

                safe_title = safe_path_name(title)
                source_article_dir = None
                
                # 获取准确的文件源目录
                for att in item.get("attachments", []):
                    if att.get("download_status") == "success":
                        source_article_dir = resolve_actual_path(att.get("local_path"))
                        if source_article_dir: break
                
                if not source_article_dir:
                    for img in item.get("images", []):
                        if img.get("download_status") == "success":
                            source_article_dir = resolve_actual_path(img.get("local_path"))
                            if source_article_dir: break

                # 拦截器 2：检查本地文件夹是否存在
                if not source_article_dir or not source_article_dir.exists():
                    if source_article_dir is not None:
                        print(f"[跳过] 找不到本地资源文件夹 -> 标题: {title[:20]}... 预期路径: {source_article_dir}")
                    continue

                # 分发路由
                target_dir = None
                if channel_name == "疾病预防控制标准":
                    category = get_cdc_category(title)
                    std_code = normalize_standard_code(title)
                    target_dir = TARGET_ROOT / "国家疾病预防控制局" / "疾病预防控制标准" / category / std_code / safe_title
                elif channel_name == "规范性文件":
                    topic = get_nhc_topic(title)
                    target_dir = TARGET_ROOT / "国家卫生健康委员会" / "规范性文件" / topic / safe_title
                elif channel_name == "政策法规":
                    category = get_policy_law_category(title)
                    target_dir = TARGET_ROOT / "国家卫生健康委员会" / "政策法规" / category / safe_title
                
                # 执行智能拷贝并修复文件后缀
                if target_dir:
                    try:
                        smart_copy_tree(source_article_dir, target_dir)
                        success_count += 1
                        if success_count % 10 == 0:
                            print(f"[进度] 已成功复制并修复 {success_count} 个文件夹...")
                    except Exception as e:
                        print(f"[报错] 复制失败 [{title[:20]}]: {e}")

    print(f"\n✅ 整体分类完毕！共成功提取、重组并修复了 {success_count} 个文章文件夹。")
    print(f"📁 结果请前往: {TARGET_ROOT.resolve()} 查阅。")

if __name__ == "__main__":
    process_documents()