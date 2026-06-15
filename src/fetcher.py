"""网页请求模块 (DrissionPage 自动越过 WAF 版)。

本模块已从 requests 升级为 DrissionPage，
通过调用真实浏览器内核，自动执行 JS 以绕过国家卫健委的 412 WAF 挑战。
"""

from __future__ import annotations

import os
import time
import mimetypes
import re
from email.message import Message
from typing import Tuple
from urllib.parse import unquote, urlparse

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
    referer: str = "",
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


def warmup_homepage(site_url: str, timeout: int = 20, browser_cookie: str = "") -> None:
    """先访问官网首页。这会让浏览器自动跑通首页的 JS 挑战，
    拿到合法的动态 Cookie，后续爬列表页就不会被拦截了。"""
    fetch_html(site_url, referer=site_url, timeout=timeout)


KNOWN_FILE_EXTENSIONS = {
    "pdf", "doc", "docx", "docm", "xls", "xlsx", "xlsm", "ppt", "pptx", "pptm",
    "wps", "et", "dps", "rtf", "csv", "txt", "zip", "rar", "7z",
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff",
}

CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed": "7z",
    "text/plain": "txt",
    "text/csv": "csv",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/webp": "webp",
    "image/tiff": "tif",
}


def _known_suffix(path_or_name: str) -> str:
    suffix = Path(path_or_name).suffix.lower().lstrip(".")
    return suffix if suffix in KNOWN_FILE_EXTENSIONS else ""


def _filename_from_content_disposition(header: str) -> str:
    if not header:
        return ""
    message = Message()
    message["content-disposition"] = header
    filename = message.get_param("filename*", header="content-disposition")
    if filename:
        if isinstance(filename, tuple):
            _, _, filename = filename
        return unquote(str(filename)).strip().strip('"')
    filename = message.get_param("filename", header="content-disposition")
    return unquote(str(filename)).strip().strip('"') if filename else ""


def _extension_from_content_type(content_type: str) -> str:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not mime:
        return ""
    if mime in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[mime]
    guessed = (mimetypes.guess_extension(mime) or "").lstrip(".").lower()
    return guessed if guessed in KNOWN_FILE_EXTENSIONS else ""


def _extension_from_magic(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if data.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    return ""


def _build_download_file_name(url: str, requested_name: str, resp: requests.Response, first_chunk: bytes) -> str:
    requested_name = requested_name or "download"
    if _known_suffix(requested_name):
        return requested_name

    header_name = _filename_from_content_disposition(resp.headers.get("content-disposition", ""))
    header_suffix = _known_suffix(header_name)
    if header_suffix:
        return f"{Path(requested_name).stem or Path(header_name).stem or 'download'}.{header_suffix}"

    url_suffix = _known_suffix(unquote(urlparse(url).path))
    if url_suffix:
        return f"{Path(requested_name).stem or 'download'}.{url_suffix}"

    content_type_suffix = _extension_from_content_type(resp.headers.get("content-type", ""))
    if content_type_suffix:
        return f"{Path(requested_name).stem or 'download'}.{content_type_suffix}"

    magic_suffix = _extension_from_magic(first_chunk)
    if magic_suffix:
        return f"{Path(requested_name).stem or 'download'}.{magic_suffix}"

    return requested_name


def download_file(url: str, save_dir: str | Path, file_name: str, timeout: int = 30) -> bool | str:
    """带 WAF 穿透的二进制文件下载器 (兼容各版本 DrissionPage)。"""
    save_dir = Path(save_dir)
    save_path = save_dir / file_name
    # 如果文件已经下载过了，直接跳过，支持断点续爬
    if save_path.exists():
        return file_name

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

        iterator = resp.iter_content(chunk_size=8192)
        first_chunk = next(iterator, b"")
        actual_file_name = _build_download_file_name(url, file_name, resp, first_chunk)
        save_path = save_dir / actual_file_name
        if save_path.exists():
            return actual_file_name

        with open(save_path, 'wb') as f:
            if first_chunk:
                f.write(first_chunk)
            for chunk in iterator:
                f.write(chunk)
        return actual_file_name
    except Exception as e:
        print(f"下载文件失败: {url}, 错误: {e}")
        return False
