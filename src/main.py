"""官网文件爬虫入口 (多站点路由分发版)。

运行方式：
    cd /d D:\LZH\A-Project\Crawler311\nhc_crawler
    python src/main.py
"""

from __future__ import annotations

import re
import importlib
import random
import time
import json
from pathlib import Path

from tqdm import tqdm

from fetcher import WafChallengeError, fetch_html, load_browser_cookie, set_browser_cookie, download_file
from utils import (
    LOG_DIR,
    OUTPUT_DIR,
    PROJECT_ROOT,
    ATTACHMENT_DIR, 
    IMAGE_DIR,
    append_jsonl,
    ensure_dirs,
    generate_doc_id,
    infer_document_type,
    infer_policy_category,
    load_json,
    save_raw_html,
    setup_logger,
    text_hash
)


CONFIG_PATH = PROJECT_ROOT / "config" / "sites.json"
LOG_PATH = LOG_DIR / "crawler.log"
WAF_HELP_LOGGED = False


def _update_item_from_detail(item: dict, detail: dict, channel: dict) -> None:
    """用详情页解析结果补充列表页初步数据。"""
    title = detail.get("title") or item.get("title", "")
    publish_date = detail.get("publish_date") or item["dates"].get("publish_date", "")
    source_department = detail.get("source_department", "")
    body_text = detail.get("body_text", "")

    item["title"] = title
    item["dates"]["publish_date"] = publish_date
    item["organization"]["source_department"] = source_department
    item["content"]["body_text"] = body_text
    item["content"]["body_html"] = detail.get("body_html", "")
    item["attachments"] = detail.get("attachments", [])
    item["classification"]["document_type"] = infer_document_type(title)
    item["classification"]["policy_category"] = infer_policy_category(
        title,
        channel.get("channel_name", ""),
        channel.get("default_policy_category", "")
    )
    item["crawl"]["text_hash"] = text_hash(body_text)
    item["raw"]["raw_title"] = item["raw"].get("raw_title") or title
    item["raw"]["raw_date"] = item["raw"].get("raw_date") or publish_date
    item["raw"]["raw_source"] = source_department


def _mark_failed(item: dict, error: Exception, status: str = "failed") -> None:
    """给单条数据标记失败状态，保证失败记录也会写入 JSONL。"""
    item["crawl"]["crawl_status"] = status
    item["crawl"]["error_message"] = str(error)


def _save_error_response(exc: Exception, doc_id: str) -> str:
    """如果异常中带有 HTTP 响应 HTML，则保存下来方便排查。"""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "text", ""):
        return save_raw_html(response.text, doc_id)
    return ""


def _log_waf_help(logger, has_cookie: bool) -> None:
    """输出 WAF 拦截的处理建议。"""
    global WAF_HELP_LOGGED
    if WAF_HELP_LOGGED:
        return
    WAF_HELP_LOGGED = True

    logger.error(
        "检测到站点返回 WAF/JS 校验页。"
        "如果是纯 requests 爬虫，不执行 JavaScript，因此无法自动通过该校验。"
    )
    if has_cookie:
        logger.error("当前已经加载 Cookie，但仍被拦截。请确认 Cookie 是否过期。")
    else:
        logger.error("请设置正确的 Cookie，或使用 DrissionPage 浏览器内核绕过。")


def run() -> None:
    """爬虫主流程（支持多站点遍历调度）。"""
    ensure_dirs()
    logger = setup_logger(LOG_PATH)
    
    # 此时加载的是一个列表 (多个站点的配置)
    sites_configs = load_json(CONFIG_PATH)
    if isinstance(sites_configs, dict):
        sites_configs = [sites_configs]  # 兼容以前的单字典格式

    for site_config in sites_configs:
        site_name = site_config.get("site_name", "未知站点")
        parser_type = site_config.get("parser_type", "nhc")
        
        # 1. 尝试动态加载当前站点的解析规则模块
        try:
            # 根据你之前的导入路径，动态拼接模块路径
            base_module_path = "parser"
            list_module = importlib.import_module(f"{base_module_path}.{parser_type}_list")
            detail_module = importlib.import_module(f"{base_module_path}.{parser_type}_detail")
            
            # 提取具体的解析函数
            parse_list_page = list_module.parse_list_page
            build_page_urls = list_module.build_page_urls
            parse_detail_page = detail_module.parse_detail_page
            
            logger.info("============== 成功加载 [%s] 解析器 ==============", site_name)
        except ModuleNotFoundError as e:
            logger.warning("跳过 [%s]：未找到对应的解析器文件 '%s_list.py' 等 (%s)", site_name, parser_type, e)
            continue
        except AttributeError as e:
            logger.warning("跳过 [%s]：解析器文件中缺少必要的函数 (%s)", site_name, e)
            continue

        # 2. 网站参数初始化
        max_pages = int(site_config.get("max_pages", 1))
        request_config = site_config.get("request", {})
        timeout = int(request_config.get("timeout", 20))
        browser_cookie = load_browser_cookie(request_config.get("browser_cookie", ""))
        set_browser_cookie(browser_cookie)
        site_url = site_config.get("site_url", "https://www.nhc.gov.cn/")

        # 动态指定当前站点的输出文件路径
        output_path = OUTPUT_DIR / f"{parser_type}_all_documents.jsonl"
        
        # --- 读取已有文件中的 id 和 url 进行去重 ---
        existing_keys = set()
        if output_path.exists():
            with open(output_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        if "doc_id" in data and data["doc_id"]:
                            existing_keys.add(data["doc_id"])
                        if "url" in data and data["url"]:
                            existing_keys.add(data["url"])
                    except json.JSONDecodeError:
                        pass
        

        # 3. 首页 warm-up (利用 fetch_html 自动访问首页过验证)
        try:
            fetch_html(site_url, referer=site_url, timeout=timeout)
            logger.info("[%s] 首页 warm-up 成功，已建立 Session/Cookie", site_name)
        except WafChallengeError as exc:
            raw_path = _save_error_response(
                exc,
                generate_doc_id(site_config.get("site_domain", ""), "homepage_waf_error", 1)
            )
            if raw_path:
                logger.info("[%s] 首页 WAF 响应已保存：%s", site_name, raw_path)
            _log_waf_help(logger, has_cookie=bool(browser_cookie))
        except Exception as exc:
            logger.warning("[%s] 首页 warm-up 失败，但程序继续执行：%s", site_name, exc)

        # 4. 获取该站点开启的栏目
        channels = [c for c in site_config.get("channels", []) if c.get("enabled", True)]
        if not channels:
            logger.info("[%s] 暂未开启任何栏目抓取", site_name)
            continue

        total_success = 0
        total_failed = 0
        logger.info("开始爬取：%s，共 %s 个栏目", site_name, len(channels))

        # 5. 遍历爬取各个栏目
        for channel in channels:
            channel_name = channel.get("channel_name", "")
            logger.info(">>> 开始爬取栏目：%s", channel_name)

            all_items: list[dict] = []
            
            # 5.1 获取列表页
            for page_index, list_url in enumerate(build_page_urls(channel.get("channel_url", ""), max_pages), start=1):
                try:
                    list_html, list_status = fetch_html(
                        list_url,
                        referer=site_url,
                        timeout=timeout
                    )
                    list_doc_id = generate_doc_id(site_config.get("site_domain", ""), f"{channel_name}_list", page_index)
                    raw_path = save_raw_html(list_html, list_doc_id)
                    logger.info("列表页请求成功：%s，状态码：%s", list_url, list_status)

                    # 调用该站点专属的解析规则
                    page_items = parse_list_page(list_html, channel, site_config)
                    logger.info("栏目 [%s] 第 %s 页解析到 %s 条", channel_name, page_index, len(page_items))
                    all_items.extend(page_items)

                    if len(page_items) == 0:
                        logger.info("栏目 [%s] 当前页未解析到任何数据，自动判断已达最后一页，停止翻页！", channel_name)
                        break  # <--- 核心指令：跳出当前列表页的翻页循环

                except Exception as exc:
                    list_doc_id = generate_doc_id(site_config.get("site_domain", ""), f"{channel_name}_list_error", page_index)
                    raw_path = _save_error_response(exc, list_doc_id)
                    total_failed += 1
                    if isinstance(exc, WafChallengeError):
                        _log_waf_help(logger, has_cookie=bool(browser_cookie))
                        logger.error("列表页被 WAF 拦截：%s", list_url)
                    else:
                        logger.exception("列表页失败：%s，错误：%s", list_url, exc)

            if not all_items:
                logger.warning("栏目 [%s] 未解析到任何列表数据", channel_name)
                continue

            # 5.2 获取详情页
            for item in tqdm(all_items, desc=f"爬取 {channel_name}", unit="条"):
                if item.get("doc_id") in existing_keys or item.get("url") in existing_keys:
                    logger.info("数据已存在，跳过解析并保留原数据：%s", item.get("title"))
                    continue

                if item["url"].lower().endswith(('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip')):
                    logger.info("检测到直链文件，直接入库跳过解析：%s", item["url"])
                    if item.get("attachments"):
                        att = item["attachments"][0]  # 从列表页解析中获取提取好的附件信息

                        safe_att_name = re.sub(r'[\\/:*?"<>|]', '_', att["name"])
                        if not safe_att_name.lower().endswith(f".{att['file_type']}"):
                            safe_att_name = f"{safe_att_name}.{att['file_type']}"

                        logger.info("正在下载直链文件: %s", safe_att_name)
                        # 调用下载器，存入附件目录
                        success = download_file(att["url"], ATTACHMENT_DIR, safe_att_name)
                        if success:
                            att["local_path"] = f"data/attachments/{safe_att_name}"
                            att["download_status"] = "success"
                        else:
                            att["download_status"] = "failed"

                    item["crawl"]["crawl_status"] = "success"
                    item["crawl"]["http_status"] = 200
                    append_jsonl(item, output_path)
                    total_success += 1
                    continue
                try:
                    detail_html, detail_status = fetch_html(
                        item["url"],
                        referer=channel.get("channel_url", site_url),
                        timeout=timeout
                    )
                    raw_path = save_raw_html(detail_html, item["doc_id"])
                    
                    # 调用该站点专属的解析规则
                    detail = parse_detail_page(detail_html, item["url"])

                    safe_site_name = re.sub(r'[\\/:*?"<>|]', '_', site_name)
                    safe_channel_name = re.sub(r'[\\/:*?"<>|]', '_', channel_name)
                    current_title = detail.get("title") or item.get("title", "未命名文章")
                    safe_article_title = re.sub(r'[\\/:*?"<>|]', '_', current_title)

                    # 截断过长标题，防止 Windows 路径整体超限报错
                    if len(safe_article_title) > 80:
                        safe_article_title = safe_article_title[:80] + "..."

                    # 自动创建三级专属文件夹
                    item_attachment_dir = ATTACHMENT_DIR / safe_site_name / safe_channel_name / safe_article_title
                    item_image_dir = IMAGE_DIR / safe_site_name / safe_channel_name / safe_article_title
                    item_attachment_dir.mkdir(parents=True, exist_ok=True)
                    item_image_dir.mkdir(parents=True, exist_ok=True)

                    # 1. 触发图片下载（直接用图片哈希名存入该文章文件夹）
                    for img_info in detail.get("images", []):
                        safe_img_name = re.sub(r'[\\/:*?"<>|]', '_', img_info["file_name"])
                        
                        success = download_file(img_info["url"], item_image_dir, safe_img_name)
                        if success:
                            img_info["local_path"] = f"data/images/{safe_site_name}/{safe_channel_name}/{safe_article_title}/{safe_img_name}"
                            img_info["download_status"] = "success"
                        else:
                            img_info["download_status"] = "failed"
                            
                    # 2. 触发正文附件下载
                    for att in detail.get("attachments", []):
                        safe_att_name = re.sub(r'[\\/:*?"<>|]', '_', att["name"])
                        if not safe_att_name.lower().endswith(f".{att['file_type']}"):
                            safe_att_name = f"{safe_att_name}.{att['file_type']}"
                        
                        logger.info("正在下载正文附件: %s", safe_att_name)
                        success = download_file(att["url"], item_attachment_dir, safe_att_name)
                        if success:
                            att["local_path"] = f"data/attachments/{safe_site_name}/{safe_channel_name}/{safe_article_title}/{safe_att_name}"
                            att["download_status"] = "success"
                        else:
                            att["download_status"] = "failed"

                    item["crawl"]["http_status"] = detail_status
                    item["crawl"]["raw_html_path"] = raw_path
                    _update_item_from_detail(item, detail, channel)
                    item["crawl"]["crawl_status"] = "success"
                    item["crawl"]["error_message"] = ""

                    append_jsonl(item, output_path)
                    total_success += 1
                    
                except Exception as exc:
                    _mark_failed(item, exc, status="detail_failed")
                    response = getattr(exc, "response", None)
                    if response is not None:
                        item["crawl"]["http_status"] = getattr(response, "status_code", None)
                        item["crawl"]["raw_html_path"] = _save_error_response(exc, f"{item['doc_id']}_error")

                    append_jsonl(item, output_path)
                    total_failed += 1
                    
                    if isinstance(exc, WafChallengeError):
                        logger.error("详情页被 WAF 拦截：%s", item.get("url", ""))
                    else:
                        logger.error("详情页失败：%s，错误：%s", item.get("url", ""), exc)

                # 礼貌访问，避免被封 IP
                time.sleep(random.uniform(1, 3))

        logger.info("[%s] 爬取完毕！成功 %s 条，失败 %s 条，数据保存至：%s\n", site_name, total_success, total_failed, output_path)


if __name__ == "__main__":
    run()