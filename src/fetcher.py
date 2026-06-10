"""网页请求模块 (DrissionPage 自动越过 WAF 版)。

本模块已从 requests 升级为 DrissionPage，
通过调用真实浏览器内核，自动执行 JS 以绕过国家卫健委的 412 WAF 挑战。
"""

from __future__ import annotations

import os
import time
from typing import Tuple

from DrissionPage import WebPage

import requests
from pathlib import Path

# 初始化浏览器对象，默认会自动寻找系统自带的 Chrome 或 Edge 内核
PAGE = WebPage()


class MockResponse:
    """用来兼容原有 requests 异常格式的模拟响应对象，防止 main.py 里的 getattr 报错。"""
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class WafChallengeError(Exception):
    """站点 WAF/JS 挑战拦截错误。"""
    def __init__(self, message: str, html: str = "", status: int = 412):
        super().__init__(message)
        self.response = MockResponse(status, html)


def _looks_like_waf_challenge(html: str) -> bool:
    """判断响应源码是否依然停留在 WAF/JS 校验页。"""
    html = html or ""
    markers = (
        "WZWS-RAY",
        "$_ts",
        "_$_y()",
        "Precondition Failed",
        "content=\"TrqC53Da"
    )
    return any(marker in html for marker in markers)


def set_browser_cookie(cookie: str) -> None:
    """使用 DrissionPage 真实浏览器后，WAF 会自动跑通 JS 并下发合法 Cookie。
    保留此空函数是为了不让 main.py 调用时报错。"""
    pass


def load_browser_cookie(config_cookie: str = "") -> str:
    """兼容原有逻辑。"""
    return (os.getenv("NHC_COOKIE") or config_cookie or "").strip()


def fetch_html(
    url: str,
    headers: dict | None = None,
    referer: str = "https://www.nhc.gov.cn/",
    timeout: int = 20,
) -> Tuple[str, int]:
    """请求网页并返回 HTML 文本和 HTTP 状态码。"""

    # 让真实的浏览器访问目标 URL
    PAGE.get(url, timeout=timeout)
    
    # 遇到 412 时，网页会自动执行 JS 并触发刷新跳转，因此需要强制等待一会儿
    time.sleep(1.5) 
    
    html = PAGE.html
    status_code = 200

    # 检查是否因为访问过于频繁，彻底触发了无法自动绕过的验证码拦截
    if _looks_like_waf_challenge(html):
        status_code = 412
        error_msg = f"{status_code} WAF/JS challenge for url: {url}"
        raise WafChallengeError(error_msg, html=html, status=status_code)

    return html, status_code


def warmup_homepage(timeout: int = 20, browser_cookie: str = "") -> None:
    """先访问官网首页。这会让浏览器自动跑通首页的 JS 挑战，
    拿到合法的动态 Cookie，后续爬列表页就不会被拦截了。"""
    fetch_html("https://www.nhc.gov.cn/", timeout=timeout)


def download_file(url: str, save_dir: str | Path, file_name: str, timeout: int = 30) -> bool:
    """带 WAF 穿透的二进制文件下载器 (兼容各版本 DrissionPage)。"""
    save_path = Path(save_dir) / file_name
    # 如果文件已经下载过了，直接跳过，支持断点续爬
    if save_path.exists():
        return True 

    # ======== 核心修复区：兼容不同版本 DrissionPage 的 Cookie 格式 ========
    try:
        raw_cookies = PAGE.cookies()
    except Exception:
        raw_cookies = PAGE.get_cookies() if hasattr(PAGE, 'get_cookies') else {}

    # 将获取到的原始 cookie 统一转换为 requests 能认的 dict 格式
    cookies_dict = {}
    if isinstance(raw_cookies, dict):
        cookies_dict = raw_cookies
    elif isinstance(raw_cookies, list):
        cookies_dict = {str(c.get("name", "")): str(c.get("value", "")) for c in raw_cookies if "name" in c}
    # ======================================================================

    headers = {"User-Agent": str(PAGE.user_agent)}
    
    try:
        # 使用 requests 流式下载大文件
        resp = requests.get(url, headers=headers, cookies=cookies_dict, stream=True, timeout=timeout)
        resp.raise_for_status()
        
        with open(save_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"下载文件失败: {url}, 错误: {e}")
        return False