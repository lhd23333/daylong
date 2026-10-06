"""朝夕的 Windows 桌面窗口入口（源码直跑）。

跑这个文件就是跑仓库里的实时源码：改 *.py 重启生效，改 static/ 刷新窗口生效，
不需要打包成 exe。壳层只做四件事——单实例、把 MusicCompanionServer 放在后台
线程、开一个 WebView2 窗口指向它、关窗后收干净；业务代码一行都不在这里。

依赖只有 pywebview（Windows 走系统自带的 WebView2 运行时，不捆绑 Chromium）。
用 tools/setup-desktop.ps1 建 .venv 装好。想要零依赖、开浏览器的方案见 server.py，
它仍然是纯标准库，桌面版不改变这一点。

端口固定 8000 而不是随机：前端的偏好存在 localStorage 里，而 localStorage 按
来源（含端口）隔离，换端口等于换一份存储，用户的设置会「凭空消失」。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8000
WINDOW_TITLE = "朝夕 · 陪你过完这一天"
APP_NAME = "朝夕"

ICON_PATH = ROOT / "assets" / "朝夕.ico"
LOG_DIR = ROOT / "data"
LOG_PATH = LOG_DIR / "desktop.log"
STDIO_PATH = LOG_DIR / "desktop.out.log"
STORAGE_PATH = LOG_DIR / "webview"

# 标题栏与窗口边框刷成页面的暖纸色，和内容区连成一片——视觉上等于把那条蓝边
# 「取消」了，同时保住最小化 / 最大化 / 关闭三个原生按钮和贴靠、系统菜单这些
# 系统行为。文字色给深褐（页面正文同色），否则跟随系统深色主题时会白字白底。
CAPTION_RGB = 0xF5F2EA
CAPTION_TEXT_RGB = 0x1E1C19

# 单实例互斥体的句柄必须活到进程结束，被回收就等于放开了锁。
_instance_guard: object = None

logger = logging.getLogger("zhaoxi.desktop")


def _setup_logging() -> None:
    """日志写 data/desktop.log；写不进去也不能让程序起不来。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except OSError:
        pass


def _redirect_stdio() -> None:
    """pythonw.exe 没有控制台，sys.stdout / sys.stderr 是 None。

    此时任何一句 print 都会以 AttributeError 崩掉，而崩在一个连控制台都没有的
    进程里，用户只会看到「窗口闪一下就没了」。所以先把两个流兜到文件里再往下走。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(STDIO_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _alert(message: str) -> None:
    """桌面上没有终端，出错只能靠弹窗告诉用户，同时落一份日志。"""
    logger.error(message)
    try:
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _acquire_single_instance() -> bool:
    """Windows 具名互斥体拦双开。

    必须有：PlaylistStore 把收藏夹整表读进内存、每次改动整表重写，两个进程
    各持一份就会互相覆盖。锁本身拿不到时放行，不能因为拦不住就不让人用。
    """
    global _instance_guard
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\ZhaoxiDesktop.SingleInstance")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _instance_guard = handle
        return True
    except Exception:
        return True


def _focus_existing_window() -> bool:
    """把已在运行的窗口提到前台。

    比弹一句「去任务栏里找它」有用得多——用户双击第二次，窗口就该自己跳出来，
    这才是软件该有的样子。提不到（窗口在别的虚拟桌面、或前台锁拒绝）就返回
    False，由调用方退回到弹窗提示。
    """
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        # 最小化的窗口要先还原，否则 SetForegroundWindow 提到的是个缩在任务栏的壳。
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _colorref(rgb: int) -> int:
    """DWM 要的颜色是 0x00BBGGRR，不是常见的 0xRRGGBB。"""
    red, green, blue = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
    return (blue << 16) | (green << 8) | red


def _apply_caption_color(window) -> None:
    """把系统标题栏刷成暖纸色。

    走 DWM 的 DWMWA_CAPTION_COLOR 一族，只在 Windows 11（build 22000+）生效；
    更老的系统上这几个属性不存在，调用会返回非 0，忽略即可——标题栏保持系统
    默认外观，不影响使用，所以这里不检查返回值也不报错。

    参数名必须是 ``window``：pywebview 的 Event 按参数名决定要不要把窗口对象
    传进来。
    """
    if os.name != "nt":
        return
    try:
        hwnd = window.native.Handle.ToInt32()
    except Exception:
        return
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        return
    for attribute, rgb in (
        (34, CAPTION_RGB),       # DWMWA_BORDER_COLOR
        (35, CAPTION_RGB),       # DWMWA_CAPTION_COLOR
        (36, CAPTION_TEXT_RGB),  # DWMWA_TEXT_COLOR
    ):
        value = ctypes.c_int(_colorref(rgb))
        dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
    logger.info("标题栏已刷成页面同色 #%06X", CAPTION_RGB)


def _is_zhaoxi_on(port: int) -> bool:
    """端口上已经有服务了，判断它是不是朝夕。"""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return resp.status == 200 and isinstance(payload, dict) and "mode" in payload
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def main() -> int:
    # 先兜 stdio 再配日志：logging 自己的兜底处理器也写 stderr，顺序反了会在
    # pythonw 下撞上同一个 None。
    _redirect_stdio()
    _setup_logging()
    logger.info("桌面版启动，源码目录 %s", ROOT)

    if not _acquire_single_instance():
        if _focus_existing_window():
            logger.info("已有实例在运行，已把它的窗口提到前台")
        else:
            _alert("朝夕已经开着一个窗口了。\n\n请到任务栏里找它；要重开就先关掉旧窗口。")
        return 0

    port = DEFAULT_PORT
    raw_port = os.environ.get("ZHAOXI_PORT", "").strip()
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            _alert(f"ZHAOXI_PORT 不是合法端口号：{raw_port!r}")
            return 1

    # 放在错误处理之后导入：pywebview 没装或 server 起不来时，走的是弹窗提示，
    # 而不是 pythonw 里一条谁也看不见的堆栈。
    import webview

    from server import MusicCompanionServer

    try:
        server = MusicCompanionServer((HOST, port))
    except OSError:
        if _is_zhaoxi_on(port):
            _alert(f"朝夕已经在运行（{HOST}:{port} 被它占着）。\n\n请在任务栏里找它。")
        else:
            _alert(
                f"启动失败：端口 {port} 被别的程序占用了。\n\n"
                f"关掉那个程序，或者设环境变量 ZHAOXI_PORT 换一个端口再启动。"
            )
        logger.exception("绑定 %s:%d 失败", HOST, port)
        return 1

    server_thread = threading.Thread(
        target=server.serve_forever, name="zhaoxi-http", daemon=True
    )
    server_thread.start()
    logger.info("本地服务已就绪：http://%s:%d", HOST, port)

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://{HOST}:{port}/",
        width=1280,
        height=860,
        # 实测坑：min_size 取得太接近初始尺寸时，窗口会被顶到 min_size 上
        # （175% 缩放下 1280x860 的窗口缩成了 960x640）。下半限只是防拖没，
        # 取小值即可，别再往上调。
        min_size=(400, 300),
        # 与页面 --paper 同色，压掉 WebView 首帧前的一下白闪。
        background_color="#f5f2ea",
        text_select=True,
    )
    # 窗口显示后才有 HWND，着色挂在这里（见 _apply_caption_color）。
    window.events.shown += _apply_caption_color

    try:
        webview.start(
            gui="edgechromium",
            debug=os.environ.get("ZHAOXI_DEBUG", "") == "1",
            # 前端偏好存在 localStorage，private_mode 一开就等于每次都是新用户。
            private_mode=False,
            storage_path=str(STORAGE_PATH),
            icon=str(ICON_PATH) if ICON_PATH.is_file() else None,
        )
    finally:
        logger.info("窗口已关闭，正在停止本地服务")
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
    return 0


def _main_guarded() -> int:
    try:
        return main()
    except Exception:
        detail = traceback.format_exc()
        logger.exception("桌面版异常退出")
        last_line = detail.strip().splitlines()[-1]
        _alert(f"朝夕启动时出错：\n{last_line}\n\n完整日志：{LOG_PATH}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main_guarded())
