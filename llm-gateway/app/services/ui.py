"""Local-only management-page helpers."""
import asyncio
import os
import sys
from urllib.parse import urlparse
import webbrowser

from fastapi import Request

from app.config import UiSettings


def is_local_or_secure_request(request: Request) -> bool:
    """仅允许本地地址或 HTTPS 请求修改运行时调试 Key。"""
    host = request.client.host if request.client else ""
    return request.url.scheme == "https" or host in {"127.0.0.1", "::1", "localhost", "testclient"}


def should_auto_open_browser(settings: UiSettings) -> bool:
    """在本地图形环境中才允许自动打开浏览器。"""
    host = urlparse(settings.url).hostname
    local_url = host in {"127.0.0.1", "localhost", "::1"}
    has_gui = sys.platform in {"darwin", "win32"} or bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
    return settings.auto_open and local_url and not os.getenv("CI") and has_gui


async def open_browser_safely(settings: UiSettings) -> None:
    """异步尝试打开浏览器；失败完全不影响服务生命周期。"""
    if not should_auto_open_browser(settings):
        return
    await asyncio.sleep(0.5)
    try:
        await asyncio.to_thread(webbrowser.open, settings.url)
    except Exception:
        return
