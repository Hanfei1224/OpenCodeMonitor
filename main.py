# -*- coding: utf-8 -*-
"""OpenCode Go 用量监控磁贴 — pywebview(WebView2) + pystray + Win32"""
import ctypes
import json
import os
import sys
import threading
import time
import urllib.request

import pystray
import webview
from PIL import Image, ImageDraw

# 冻结(打包)模式：config.json 放 exe 旁供用户编辑；index.html 从打包资源目录读
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR
CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
INDEX_FILE = os.path.join(RES_DIR, 'index.html')
USAGE_URL = 'https://opencode.ai/zen/go/v1/usage'
DEFAULT_CONFIG = {
    "api_key": "", "plan_name": "OpenCode Go", "refresh_seconds": 60,
    "bg_color": "#1e2330", "bg_opacity": 1.0, "accent_color": "#5b9bff",
    "window_x": None, "window_y": None,  # 上次退出的窗口位置，下次启动恢复
    "topmost": True, "click_through": True,  # 置顶/穿透状态，下次启动恢复
}

# ---- Win32 ----
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080  # 工具窗口：任务栏与 Alt+Tab 都不显示
WS_EX_APPWINDOW = 0x00040000  # WinForms 自动设置，会强制窗口进任务栏，必须清掉
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

# 64 位正确传递句柄/指针，避免 HWND_TOPMOST(-1) 被截断导致 SetWindowPos 失败
_user32 = ctypes.windll.user32
_user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_void_p
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.SetWindowPos.restype = ctypes.c_bool


class RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
_user32.GetWindowRect.restype = ctypes.c_bool
_user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
_user32.GetCursorPos.restype = ctypes.c_bool


class State:
    topmost = True
    click_through = True


state = State()
window = None
icon = None
STOP = threading.Event()
WAKE = threading.Event()  # 保存 API Key 后唤醒 fetch_loop 立即拉取
SETTING_KEYS = ('bg_color', 'bg_opacity', 'accent_color')
cache = {
    'usage': None, 'last_refresh': None, 'error': None, 'latency_ms': None,
    'plan_name': DEFAULT_CONFIG['plan_name'],
    'settings': {k: DEFAULT_CONFIG[k] for k in SETTING_KEYS},
}
lock = threading.Lock()
_pos_saved = [0.0]  # 拖拽期间窗口位置写入 config 的节流时间戳


# ---- config ----
def load_config():
    if not os.path.exists(CONFIG_FILE):
        cfg = DEFAULT_CONFIG.copy()
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return cfg
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        changed = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
                changed = True
        if changed:
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
        return cfg
    except (OSError, ValueError):
        return DEFAULT_CONFIG.copy()


def save_window_pos(x, y, force=False):
    # 窗口位置写入 config.json（物理像素，与 GetWindowRect/SetWindowPos 一致）。force 用于退出时落盘
    now = time.time()
    if not force and now - _pos_saved[0] < 1.0:
        return
    _pos_saved[0] = now
    cfg = load_config()
    if cfg.get('window_x') == x and cfg.get('window_y') == y:
        return
    cfg['window_x'] = int(x)
    cfg['window_y'] = int(y)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def save_toggles():
    # 置顶/穿透状态写入 config.json，下次启动恢复
    cfg = load_config()
    if cfg.get('topmost') != state.topmost or cfg.get('click_through') != state.click_through:
        cfg['topmost'] = state.topmost
        cfg['click_through'] = state.click_through
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def save_on_exit():
    # 退出前记录窗口位置。置顶/穿透不在此落盘（避免 API Key 面板临时关穿透被误存），只在托盘切换时保存
    hwnd = get_hwnd()
    if hwnd:
        rc = RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            save_window_pos(rc.left, rc.top, force=True)


# ---- fetch usage from opencode.ai ----
def fetch_usage(api_key, timeout=10):
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': f'Bearer {api_key}',
        'x-api-key': api_key,
        'Accept': 'application/json',
        # opencode.ai 网关会拦掉 Python-urllib 默认 UA
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/131.0.0.0 Safari/537.36'),
    })
    t0 = time.monotonic()  # 测量响应耗时（毫秒），供界面显示
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return data, int(round((time.monotonic() - t0) * 1000))


def fetch_loop():
    while not STOP.is_set():
        cfg = load_config()
        key = (cfg.get('api_key') or '').strip()
        with lock:
            cache['plan_name'] = cfg.get('plan_name') or DEFAULT_CONFIG['plan_name']
            cache['settings'] = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in SETTING_KEYS}
        if not key:
            with lock:
                cache['usage'] = None
                cache['last_refresh'] = None
                cache['latency_ms'] = None
                cache['error'] = 'config_missing'
        else:
            try:
                data, latency = fetch_usage(key)
                with lock:
                    cache['usage'] = data.get('usage')
                    cache['last_refresh'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    cache['latency_ms'] = latency
                    cache['error'] = None
            except Exception as exc:
                msg = str(exc)
                if any(t in msg for t in ('401', '403', 'AuthError', 'Invalid API key', 'Missing API key')):
                    msg = 'API Key 无效'
                with lock:
                    cache['error'] = msg
                    cache['latency_ms'] = None
        # 等待下一轮：被 WAKE.set() 唤醒则立即重取（如刚保存了 API Key）
        WAKE.wait(int(cfg.get('refresh_seconds') or 60))
        WAKE.clear()


# ---- JS bridge ----
class Api:
    def __init__(self):
        self._drag = None  # {ox, oy, cx, cy}

    def get_status(self):
        with lock:
            return {
                'usage': cache['usage'],
                'last_refresh': cache['last_refresh'],
                'latency_ms': cache['latency_ms'],
                'error': cache['error'],
                'plan_name': cache['plan_name'],
                'topmost': state.topmost,
                'click_through': state.click_through,
                'settings': dict(cache['settings']),
            }

    def save_settings(self, settings):
        cfg = load_config()
        for k in SETTING_KEYS:
            if k in settings:
                cfg[k] = settings[k]
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            with lock:
                cache['settings'] = {k: cfg[k] for k in SETTING_KEYS}
        except OSError:
            pass
        return True

    def save_api_key(self, key):
        cfg = load_config()
        cfg['api_key'] = (key or '').strip()
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        WAKE.set()  # 唤醒 fetch_loop 立即拉取，无需等下一个刷新周期
        return True

    def set_click_through(self, on):
        # 配置面板显示时临时关闭穿透，否则输入框无法点击
        apply_click_through(bool(on))
        push_state()
        return True

    def start_drag(self):
        # 记录窗口原始位置 + 光标起始位置，move_window 按增量移动（DPI 虚拟化下坐标单位不同，需乘缩放系数）
        hwnd = get_hwnd()
        if not hwnd:
            return True
        try:
            rc = RECT()
            if not _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                return True
            pt = POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            self._drag = {'ox': rc.left, 'oy': rc.top, 'cx': pt.x, 'cy': pt.y}
        except Exception:
            pass
        return True

    def move_window(self, dx, dy):
        hwnd = get_hwnd()
        if not hwnd or not self._drag:
            return True
        try:
            pt = POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            # 进程已被 WebView2 设为 DPI-aware，GetCursorPos/SetWindowPos 均为物理像素，直接 1:1 增量
            nx = self._drag['ox'] + (pt.x - self._drag['cx'])
            ny = self._drag['oy'] + (pt.y - self._drag['cy'])
            _user32.SetWindowPos(hwnd, 0, nx, ny, 0, 0,
                                 SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            save_window_pos(nx, ny)
        except Exception:
            pass
        return True

    def hide_to_tray(self):
        try:
            window.hide()
        except Exception:
            pass
        return True

    def quit(self):
        stop_all()
        return True


# ---- Win32 window helpers ----
def get_hwnd():
    try:
        native = getattr(window, 'native', None)
        h = getattr(native, 'Handle', None)
        return h.ToInt64() if h is not None else None
    except Exception:
        return None


def _get_exstyle(hwnd):
    v = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    return v & 0xFFFFFFFF


def _set_exstyle(hwnd, ex):
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex & 0xFFFFFFFF)


def _on_screen(x, y, w, h):
    # 防止恢复到已移除/改分辨率的显示器外导致窗口丢失；混合 DPI 下 GetSystemMetrics 不够精确，够用
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    u = ctypes.windll.user32
    vx = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    m = 40  # 窗口至少露出 40px
    return (x + w > vx + m and y + h > vy + m and x < vx + vw - m and y < vy + vh - m)


def apply_click_through(on):
    hwnd = get_hwnd()
    if not hwnd:
        return
    ex = _get_exstyle(hwnd)
    if on:
        ex |= (WS_EX_TRANSPARENT | WS_EX_LAYERED)
    else:
        # 关闭穿透只去掉鼠标透传位(TRANSPARENT)，必须保留 LAYERED 维持透明合成深色外观，
        # 否则半透明 HTML 直接透出桌面，浅色桌面下小部件变白（"白色覆盖"）
        ex = (ex & ~WS_EX_TRANSPARENT) | WS_EX_LAYERED
    _set_exstyle(hwnd, ex)
    state.click_through = on


def apply_topmost(on):
    hwnd = get_hwnd()
    if not hwnd:
        return
    on = bool(on)
    # 同步 WinForms 托管 TopMost 状态。只改原生 WS_EX_TOPMOST 位的话，WinForms 后续在
    # Show/Activate/重建句柄时仍会用缓存的 TopMost=False 覆盖——冷启动 WebView2 初始化慢，
    # 窗口晚于本次应用才真正显示，置顶就丢了；必须让托管状态一致才不会被回写抹掉。
    try:
        native = getattr(window, 'native', None)
        if native is not None:
            native.TopMost = on
    except Exception:
        pass
    ex = _get_exstyle(hwnd)
    ex = ex | WS_EX_TOPMOST if on else ex & ~WS_EX_TOPMOST
    _set_exstyle(hwnd, ex)
    _user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    state.topmost = on


def hide_from_taskbar():
    # 工具窗口样式：从任务栏和 Alt+Tab 隐藏，只留托盘图标操作。
    # WinForms 默认 ShowInTaskbar=True 会挂 WS_EX_APPWINDOW(0x40000)，抵消 TOOLWINDOW 让窗口
    # 仍进任务栏，所以必须清掉该位（实测 exstyle 0xD00A8 里就带着它）。
    hwnd = get_hwnd()
    if not hwnd:
        return
    ex = _get_exstyle(hwnd)
    _set_exstyle(hwnd, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)


def push_state():
    try:
        window.evaluate_js(
            "window.applyState({topmost:%s,click_through:%s})"
            % (json.dumps(bool(state.topmost)), json.dumps(bool(state.click_through)))
        )
    except Exception:
        pass


def show_window():
    try:
        window.show()
    except Exception:
        pass
    hide_from_taskbar()  # show/hide 时序可能复位 exstyle，重挂工具窗口位


# ---- tray ----
def make_icon():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=(30, 36, 52, 255), outline=(96, 140, 255, 255), width=3)
    d.arc([14, 14, 50, 50], start=-90, end=110, fill=(96, 140, 255, 255), width=5)
    return img


def tray_thread():
    global icon
    menu = pystray.Menu(
        pystray.MenuItem('显示窗口', lambda item: show_window()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda item: '置顶' + ('  ✓' if state.topmost else ''),
            lambda item: (apply_topmost(not state.topmost), push_state(), save_toggles()),
            checked=lambda item: state.topmost,
        ),
        pystray.MenuItem(
            lambda item: '鼠标穿透' + ('  ✓' if state.click_through else ''),
            lambda item: (apply_click_through(not state.click_through), push_state(), save_toggles()),
            checked=lambda item: state.click_through,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出', lambda item: stop_all()),
    )
    icon = pystray.Icon('opencode-monitor', make_icon(), 'OpencodeMonitor', menu)
    icon.run()


def stop_all():
    STOP.set()
    save_on_exit()  # 退出前记录窗口位置（置顶/穿透在托盘切换时已落盘）
    global icon
    if icon is not None:
        try:
            icon.stop()
        except Exception:
            pass
    try:
        window.destroy()
    except Exception:
        pass


# ---- boot ----
def on_start():
    # native 由 GUI 线程在创建窗口时赋值，本回调线程可能先跑，轮询等待
    for _ in range(200):
        if window.native is not None:
            break
        time.sleep(0.1)
    cfg = load_config()
    # 从 config 恢复置顶/穿透状态
    state.topmost = bool(cfg.get('topmost', True))
    state.click_through = bool(cfg.get('click_through', True))
    apply_click_through(state.click_through)
    apply_topmost(state.topmost)
    hide_from_taskbar()

    # 恢复到上次退出位置（与拖拽同一套物理像素坐标）
    sx, sy = cfg.get('window_x'), cfg.get('window_y')
    hwnd = get_hwnd()
    if hwnd and sx is not None and sy is not None:
        rc = RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if _on_screen(int(sx), int(sy), w, h):
                _user32.SetWindowPos(hwnd, 0, int(sx), int(sy), 0, 0,
                                     SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

    # 页面真正加载完成（窗口已稳定显示）后再重挂样式。冷启动 WebView2 初始化慢，固定 sleep
    # 可能在窗口真正显示前就重挂、随后又被覆盖；改等 loaded 事件，超时兜底。
    def _reauth():
        try:
            window.events.loaded.wait(20)
        except Exception:
            time.sleep(3)
        apply_click_through(state.click_through)
        apply_topmost(state.topmost)
        hide_from_taskbar()
    threading.Thread(target=_reauth, daemon=True).start()

    threading.Thread(target=fetch_loop, daemon=True).start()
    threading.Thread(target=tray_thread, daemon=True).start()


def main():
    global window
    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
        html = f.read()
    # 把保存的主题设置以 <style> 覆盖 :root 变量注入，渲染前立即生效，避免默认色闪烁
    cfg = load_config()
    settings = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in SETTING_KEYS}

    def _rgb(h):
        h = h.lstrip('#')
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    r, g, b = _rgb(settings['bg_color'])
    tr, tg, tb = (min(255, r + 24), min(255, g + 24), min(255, b + 24))
    inject = (
        "<style>:root{"
        f"--accent:{settings['accent_color']};"
        f"--bg-r:{r};--bg-g:{g};--bg-b:{b};"
        f"--bg-tr:{tr};--bg-tg:{tg};--bg-tb:{tb};"
        f"--bg-o:{settings['bg_opacity']};"
        "}</style>"
    )
    html = html.replace('</head>', inject + '</head>')
    window = webview.create_window(
        'OpencodeMonitor',
        html=html,
        js_api=Api(),
        width=380, height=300,
        # 置顶/穿透交给 Win32 手动管理；拖拽用 WM_NCLBUTTONDOWN 方案，不用 easy_drag
        frameless=True, transparent=True, on_top=False, easy_drag=False,
        background_color='#10131c',
    )

    def _on_closing():
        # 窗口真正销毁前记录最终位置 + 置顶/穿透状态（覆盖节流/未切换时未落盘的部分）
        save_on_exit()
    window.events.closing += _on_closing

    webview.start(on_start, icon=os.path.join(RES_DIR, 'icon.ico'))
    stop_all()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(os.path.join(APP_DIR, 'app.log'), 'w', encoding='utf-8') as f:
                f.write(traceback.format_exc())
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(0, '启动失败，详情见 app.log', 'OpencodeMonitor', 0x10)
        raise
