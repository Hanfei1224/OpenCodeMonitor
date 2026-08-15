# -*- coding: utf-8 -*-
"""OpenCode Go 用量监控磁贴 — pywebview(WebView2) + pystray + Win32"""
import ctypes
import glob
import json
import os
import sys
import threading
import time
import datetime
import sqlite3
import calendar
from pathlib import Path
import urllib.request

import pystray
import webview
from PIL import Image, ImageDraw
import clr  # pythonnet：把 WinForms 操作 marshal 到 UI 线程
from System import Action

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
    "stats_window_x": None, "stats_window_y": None,  # 用量统计窗口位置，下次打开恢复
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
stats_window = None
stats_cache = {'data': {}, 'error': None, 'last_refresh': None, 'months': None,
               'month_data': None}
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
_stats_pos_saved = [0.0]  # 用量统计窗口位置的节流时间戳


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


def _save_pos(x, y, kx, ky, throttle, force=False):
    # 窗口位置写入 config.json（物理像素，与 GetWindowRect/SetWindowPos 一致）。force 用于退出时落盘
    now = time.time()
    if not force and now - throttle[0] < 1.0:
        return
    throttle[0] = now
    cfg = load_config()
    if cfg.get(kx) == x and cfg.get(ky) == y:
        return
    cfg[kx] = int(x)
    cfg[ky] = int(y)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def save_window_pos(x, y, force=False):
    _save_pos(x, y, 'window_x', 'window_y', _pos_saved, force)


def save_stats_pos(x, y, force=False):
    _save_pos(x, y, 'stats_window_x', 'stats_window_y', _stats_pos_saved, force)


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
    # 退出前记录主窗口与用量统计窗口位置。置顶/穿透不在此落盘（避免 API Key 面板临时关穿透被误存），只在托盘切换时保存
    hwnd = get_hwnd()
    if hwnd:
        rc = RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            save_window_pos(rc.left, rc.top, force=True)
    shwnd = get_hwnd(stats_window) if stats_window is not None else None
    if shwnd:
        rc = RECT()
        if _user32.GetWindowRect(shwnd, ctypes.byref(rc)):
            save_stats_pos(rc.left, rc.top, force=True)


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


# ---- token 统计采集循环 ----
_last_stats_scan = [0.0]  # 全量采集节流时间戳（采集数秒，最多每 STATS_SCAN_INTERVAL 一次）
STATS_SCAN_INTERVAL = 60


def refresh_stats(force=False):
    # 综合采集全量重扫：opencode.db + Claude Code JSONL，一次读取同时算出最近天数视图、
    # 有消耗月份列表与按月视图缓存。增量游标会漏掉"先启动、后完成"的请求（tokens 在完成时
    # 才写入，其时间早于游标），故全量。force：启动首轮立即采集；平时受节流限制，避免
    # 5s/10s 刷新时频繁全扫卡顿。按月视图缓存后，统计窗口切月直接读内存，无感切换。
    now = time.time()
    if not force and now - _last_stats_scan[0] < STATS_SCAN_INTERVAL:
        return
    _last_stats_scan[0] = now
    try:
        rows = scan_all_rows()
        cutoff = int(time.time() * 1000) - STATS_DAYS * 86400_000
        recent = [r for r in rows if r['ts'] >= cutoff]
        cur = aggregate_days(recent)
        stats_cache['data'] = add_alpha(fill_days(cur, STATS_DAYS))
        stats_cache['months'] = _months_from_rows(rows)
        stats_cache['month_data'] = _aggregate_months(rows)  # 原子替换，读方永不看到半成品
        stats_cache['last_refresh'] = time.strftime('%Y-%m-%d %H:%M:%S')
        stats_cache['error'] = None
    except Exception as e:
        stats_cache['error'] = str(e)
    # 统计窗口按需拉取数据（get_month_stats / get_status 读 stats_cache），无需主动推送
    # （旧的 renderStats 已随每日图废弃，stats.html 无此函数）


def _today_stats():
    # 主窗口「今日」分块数据：取 stats_cache 里今天的聚合（fill_days 已含今天）
    today = datetime.date.today().isoformat()
    g = stats_cache['data'].get(today)
    if g is None:
        return {'total': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'hit_rate': 0.0}
    return {'total': g['total'], 'input': g['input'], 'output': g['output'],
            'cache_read': g['cache_read'], 'hit_rate': g['hit_rate']}


def stats_loop():
    refresh_stats(force=True)  # 启动立即采集，避免打开统计/主窗口首帧无数据
    while not STOP.is_set():
        WAKE.wait(int(load_config().get('refresh_seconds') or 60))
        WAKE.clear()
        refresh_stats()


# ---- JS bridge ----
class Api:
    def __init__(self, win_id='main'):
        self._win_id = win_id
        self._drag = None

    def _win(self):
        return window if self._win_id == 'main' else stats_window

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
                'today_stats': _today_stats(),
                'refresh_seconds': int(load_config().get('refresh_seconds') or 60),
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

    def toggle_topmost(self):
        # 主窗口置顶按钮点击：切换并持久化
        apply_topmost(not state.topmost)
        push_state()
        save_toggles()
        return state.topmost

    def toggle_click_through(self):
        # 主窗口穿透按钮点击：切换并持久化
        apply_click_through(not state.click_through)
        push_state()
        save_toggles()
        return state.click_through

    def start_drag(self):
        # 记录窗口原始位置 + 光标起始位置，move_window 按增量移动（DPI 虚拟化下坐标单位不同，需乘缩放系数）
        hwnd = get_hwnd(self._win())
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
        hwnd = get_hwnd(self._win())
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
            if self._win_id == 'main':
                save_window_pos(nx, ny)
            else:
                save_stats_pos(nx, ny)
        except Exception:
            pass
        return True

    def hide_to_tray(self):
        try:
            self._win().hide()
        except Exception:
            pass
        return True

    def get_stats(self):
        with lock:
            return dict(stats_cache)

    def set_refresh_seconds(self, seconds):
        # 只接受预设刷新间隔，非法值拒绝（防注入）。写 config 后唤醒采集循环立即按新间隔重排
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return False
        if seconds not in (5, 10, 30, 60):
            return False
        cfg = load_config()
        cfg['refresh_seconds'] = seconds
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            return False
        WAKE.set()  # 唤醒 fetch_loop / stats_loop 立即用新间隔重排
        return True

    def get_month_stats(self, year=None, month=None):
        # 用量统计窗口：按月查询聚合。缺省当前月；非法输入回退当前月
        today = datetime.date.today()
        try:
            y = int(year) if year else today.year
            m = int(month) if month else today.month
            if not 1 <= m <= 12:
                y, m = today.year, today.month
        except (TypeError, ValueError):
            y, m = today.year, today.month
        try:
            view = build_month_view(y, m)
            view['available_months'] = available_months()
            return view
        except FileNotFoundError as e:
            return {'year': y, 'month': m, 'max_total': 0, 'days': {},
                    'error': str(e), 'available_months': []}

    def show_stats(self):
        # 注意：方法体内 show_stats() 解析到模块级函数（toggle），不是本方法
        show_stats()
        return True

    def quit(self):
        stop_all()
        return True


# ---- Win32 window helpers ----
def all_windows():
    ws = [window]
    if stats_window is not None:
        ws.append(stats_window)
    return ws


def get_hwnd(target=None):
    try:
        w = target if target is not None else window
        native = getattr(w, 'native', None)
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
    for w in all_windows():
        hwnd = get_hwnd(w)
        if not hwnd:
            continue
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
    on = bool(on)
    for w in all_windows():
        hwnd = get_hwnd(w)
        if not hwnd:
            continue
        # 同步 WinForms 托管 TopMost 状态。只改原生 WS_EX_TOPMOST 位的话，WinForms 后续在
        # Show/Activate/重建句柄时仍会用缓存的 TopMost=False 覆盖——冷启动 WebView2 初始化慢，
        # 窗口晚于本次应用才真正显示，置顶就丢了；必须让托管状态一致才不会被回写抹掉。
        # 注意：Form.TopMost 必须在 UI 线程设置。本函数常被非 UI 线程（js _call / Timer）调用，
        # 直接赋值会因 pythonnet 持有 GIL 进 .NET 调用而与消息循环互相等待死锁 → 用 BeginInvoke 异步排队。
        try:
            native = getattr(w, 'native', None)
            if native is not None:
                native.BeginInvoke(Action(lambda n=native, v=bool(on): setattr(n, 'TopMost', v)))
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
    # 仍进任务栏，所以必须清掉该位。
    for w in all_windows():
        hwnd = get_hwnd(w)
        if not hwnd:
            continue
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


def _hex_rgb(h):
    h = (h or '').lstrip('#')
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (30, 35, 48)


def _ensure_stats_window():
    # 按需创建统计窗口。pywebview 在非主线程调用 create_window 且 start 已执行时会
    # 立即创建并显示窗口（webview/__init__.py:418）。启动时预建 hidden 的 WebView2 窗口
    # 会卡死（ExecuteScriptAsync 在 UI 线程永久阻塞），故改为点击才创建。
    global stats_window
    if stats_window is not None:
        return stats_window
    try:
        with open(os.path.join(RES_DIR, 'stats.html'), 'r', encoding='utf-8') as f:
            stats_html = f.read()
        # 注入保存的主题配色：<script> 给 JS 提供 accent/bg，<style> 覆盖默认 body 底色，
        # 首帧即用户设置色，避免先默认色再变强调色的闪烁
        cfg = load_config()
        settings = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in SETTING_KEYS}
        r, g, b = _hex_rgb(settings['bg_color'])
        stats_html = stats_html.replace(
            '</head>',
            '<script>window.__STATS_THEME__={accent_color:"%s",bg_color:"%s"}</script>'
            '<style>body{background:rgba(%d,%d,%d,1)}</style></head>'
            % (settings['accent_color'], settings['bg_color'], r, g, b))
        # 恢复到上次退出位置。config 存物理像素(GetWindowRect)；create_window 的 x/y 是
        # 逻辑/DPI 单位(实测×系统 DPI/96 缩放)，故先算在屏校验、再换算成逻辑坐标传入
        sx, sy = cfg.get('stats_window_x'), cfg.get('stats_window_y')
        x = y = None
        if sx is not None and sy is not None:
            try:
                scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
            except Exception:
                scale = 1.0
            if _on_screen(int(sx), int(sy), int(round(400 * scale)), int(round(450 * scale))):
                x, y = int(round(sx / scale)), int(round(sy / scale))
        w = webview.create_window(
            '用量统计', html=stats_html, js_api=Api('stats'),
            width=400, height=450, x=x, y=y, frameless=True, transparent=True,
            on_top=False, easy_drag=False, background_color='#10131c')
        stats_window = w
        # 立即套用当前置顶/穿透状态
        apply_click_through(state.click_through)
        apply_topmost(state.topmost)
        hide_from_taskbar()
    except Exception:
        stats_window = None  # stats.html 缺失等：静默，下次点击重试
    return stats_window


def show_stats():
    # 一键切换：显示 ↔ 隐藏统计窗口；未创建则创建并显示
    w = _ensure_stats_window()
    if w is None:
        return
    try:
        native = getattr(w, 'native', None)
        if native is not None and native.Visible:
            w.hide()
        else:
            w.show()
    except Exception:
        pass
    hide_from_taskbar()  # show/hide 时序可能复位 exstyle，重挂工具窗口位


# ---- token 每日统计（读 opencode 本地库）----
STATS_DAYS = 90


def opencode_db_path():
    return str(Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db')


# ---- Claude Code 会话采集（综合统计第二数据源）----
def claude_code_dir():
    """Claude Code 会话 JSONL 目录：~/.claude/projects/<项目>/<会话>.jsonl"""
    return str(Path.home() / '.claude' / 'projects')


def _iso_to_ms(ts):
    """ISO 8601 时间戳（可带 Z / 时区偏移）→ epoch 毫秒。解析失败返回 None。"""
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _claude_rows(start_ms, end_ms=None):
    """扫描 Claude Code ~/.claude/projects/**/*.jsonl，提取 assistant 消息的 usage token 数据。
    返回与 _rows_from_messages 同构的行列表（含 total/input/output/cache_read/cache_write）。
    total = input + output + cache_creation + cache_read；total<=0 的行跳过。
    mtime 早于 start_ms 的整文件跳过（jsonl 只追加，旧文件不可能含新数据）。"""
    rows = []
    d = claude_code_dir()
    if not os.path.isdir(d):
        return rows
    for fp in glob.glob(os.path.join(d, '**', '*.jsonl'), recursive=True):
        try:
            if os.path.getmtime(fp) * 1000 < start_ms:
                continue
            with open(fp, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line[0] != '{':
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if j.get('type') != 'assistant':
                        continue
                    u = (j.get('message') or {}).get('usage') or {}
                    if not u:
                        continue
                    ms = _iso_to_ms(j.get('timestamp') or '')
                    if ms is None or ms < start_ms or (end_ms is not None and ms >= end_ms):
                        continue
                    inp = int(u.get('input_tokens') or 0)
                    out = int(u.get('output_tokens') or 0)
                    cw = int(u.get('cache_creation_input_tokens') or 0)
                    cr = int(u.get('cache_read_input_tokens') or 0)
                    total = inp + out + cw + cr
                    if total <= 0:
                        continue
                    rows.append({'ts': ms, 'total': total, 'input': inp, 'output': out,
                                 'cache_read': cr, 'cache_write': cw})
        except OSError:
            continue
    return rows


def _months_from_rows(rows):
    """从请求行列表推导"有消耗的年月"（total>0），始终含当前月，倒序。"""
    months = set()
    for r in rows:
        if r['total'] > 0:
            months.add(datetime.datetime.fromtimestamp(r['ts'] / 1000).strftime('%Y-%m'))
    now = datetime.datetime.now()
    months.add(now.strftime('%Y-%m'))
    out = []
    for m in months:
        if '-' in m:
            y, mm = m.split('-')
            out.append([int(y), int(mm)])
    out.sort(key=lambda ym: (ym[0], ym[1]), reverse=True)
    return out


def _rows_from_messages(cursor):
    """把 message 表 data 列解析为请求列表。单条缺失/损坏跳过。"""
    rows = []
    for (data,) in cursor:
        try:
            j = json.loads(data)
            toks = j.get('tokens') or {}
            if not toks.get('total'):
                continue
            c = toks.get('cache') or {}
            t = j.get('time') or {}
            if not t.get('created'):
                continue
            rows.append({
                'ts': int(t.get('created') or 0),
                'total': int(toks.get('total') or 0),
                'input': int(toks.get('input') or 0),
                'output': int(toks.get('output') or 0),
                'cache_read': int(c.get('read') or 0),
                'cache_write': int(c.get('write') or 0),
            })
        except (ValueError, TypeError):
            continue
    return rows


def scan_all_rows():
    """全历史综合采集：opencode.db(message 表, 全部) + Claude Code 会话 JSONL(全部)。
    一次扫描同时服务最近天数视图与按月视图。任一源异常不影响另一源。"""
    rows = []
    db_path = opencode_db_path()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
            try:
                cur = conn.execute(
                    "SELECT data FROM message"
                    " WHERE json_extract(data, '$.tokens') IS NOT NULL")
                rows.extend(_rows_from_messages(cur))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    try:
        rows.extend(_claude_rows(0))
    except Exception:
        pass
    return rows


def _aggregate_months(rows):
    """rows → {(year, month): {date: day_stats}}，全历史按月分桶（含 hit_rate）。
    统计窗口切月直接读它，避免每次切月重扫文件。"""
    months = {}
    for r in rows:
        d = datetime.date.fromtimestamp(r['ts'] / 1000)
        dm = months.setdefault((d.year, d.month), {})
        g = dm.get(d.isoformat())
        if g is None:
            g = dm[d.isoformat()] = {'total': 0, 'input': 0, 'output': 0,
                                     'cache_read': 0, 'cache_write': 0, 'requests': 0,
                                     'hit_rate': 0.0}
        g['total'] += r['total']; g['input'] += r['input']; g['output'] += r['output']
        g['cache_read'] += r['cache_read']; g['cache_write'] += r['cache_write']
        g['requests'] += 1
    for dm in months.values():
        for g in dm.values():
            denom = g['cache_read'] + g['input']
            g['hit_rate'] = round(g['cache_read'] / denom, 4) if denom else 0.0
    return months


def read_daily_usage(days=STATS_DAYS):
    """综合采集：opencode.db(message 表) + Claude Code 会话 JSONL。
    返回最近 days 天全量请求列表。任一源缺失/异常不影响另一源。"""
    cutoff = int(time.time() * 1000) - days * 86400_000
    rows = []
    db_path = opencode_db_path()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
            try:
                cur = conn.execute(
                    "SELECT data FROM message"
                    " WHERE json_extract(data, '$.tokens') IS NOT NULL"
                    " AND json_extract(data, '$.time.created') >= ?", [cutoff])
                rows.extend(_rows_from_messages(cur))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    try:
        rows.extend(_claude_rows(cutoff))
    except Exception:
        pass
    return rows


def read_month_usage(year, month):
    """综合采集指定年月落在 [月首, 次月首) 的请求列表（opencode.db + Claude Code）。"""
    start = int(datetime.datetime(year, month, 1).timestamp() * 1000)
    nxt = int((datetime.datetime(year, month, 1) + datetime.timedelta(days=32))
              .replace(day=1).timestamp() * 1000)
    rows = []
    db_path = opencode_db_path()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
            try:
                cur = conn.execute(
                    "SELECT data FROM message"
                    " WHERE json_extract(data, '$.tokens') IS NOT NULL"
                    " AND json_extract(data, '$.time.created') >= ?"
                    " AND json_extract(data, '$.time.created') < ?", [start, nxt])
                rows.extend(_rows_from_messages(cur))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    try:
        rows.extend(_claude_rows(start, nxt))
    except Exception:
        pass
    return rows


def build_month_view(year, month):
    """聚合指定年月：优先读 stats_cache['month_data']（refresh_stats 全量采集时建好），
    冷缓存/缺月才回退单月扫描。返回 {year, month, max_total, days:{date: day_stats(含alpha)}}。
    补全该月全部天；alpha 按该月内最大单日 total 归一化（无数据 → 0.12）。"""
    dm = (stats_cache.get('month_data') or {}).get((year, month))
    if dm is None:
        dm = aggregate_days(read_month_usage(year, month))
    n_days = calendar.monthrange(year, month)[1]
    out = {}
    for d in range(1, n_days + 1):
        key = datetime.date(year, month, d).isoformat()
        g = dm.get(key)
        out[key] = g if g is not None else {
            'total': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0,
            'requests': 0, 'hit_rate': 0.0}
    add_alpha(out)
    mx = max((g['total'] for g in out.values()), default=0)
    return {'year': year, 'month': month, 'max_total': mx, 'days': out}


def available_months():
    """返回有 token 消耗的年月列表（倒序，如 [[2026,8],[2026,7]]）。
    综合两数据源，只统计 total>0 的月份；始终含当前月。优先用 stats_loop 的缓存，避免反复全扫。"""
    cached = stats_cache.get('months')
    if cached:
        return [list(m) for m in cached]
    return _months_from_rows(read_daily_usage(STATS_DAYS))


def aggregate_days(rows):
    """按 time.created 归入 'YYYY-MM-DD'，返回 {date: day_stats}。
    hit_rate = cache_read/(cache_read+input)，分母 0 → 0。"""
    days_map = {}
    for r in rows:
        d = datetime.date.fromtimestamp(r['ts'] / 1000).isoformat()
        g = days_map.get(d)
        if g is None:
            g = days_map[d] = {'total': 0, 'input': 0, 'output': 0, 'cache_read': 0,
                               'cache_write': 0, 'requests': 0, 'hit_rate': 0.0}
        g['total'] += r['total']; g['input'] += r['input']; g['output'] += r['output']
        g['cache_read'] += r['cache_read']; g['cache_write'] += r['cache_write']
        g['requests'] += 1
    for g in days_map.values():
        denom = g['cache_read'] + g['input']
        g['hit_rate'] = round(g['cache_read'] / denom, 4) if denom else 0.0
    return days_map


def add_alpha(days_map):
    """给每天加 alpha（强调色透明度，0.12~1.0 连续渐变）。
    ratio=(total/max)^0.7，max 为窗口内最大单日 total；total=0 → alpha=0.12。"""
    mx = max((g['total'] for g in days_map.values()), default=0)
    for g in days_map.values():
        ratio = (g['total'] / mx) ** 0.7 if mx else 0.0
        # 颜色上限 0.85：全浓时仍透出暗底，保证格内日期数字可读
        g['alpha'] = round(min(0.85, 0.12 + 0.88 * ratio), 3)
    return days_map


def fill_days(days_map, days=STATS_DAYS):
    """补全最近 days 天（升序，含空天 total=0/alpha=0.12），返回 {date: day_stats}。"""
    out = {}
    today = datetime.date.today()
    for i in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        g = days_map.get(d)
        out[d] = g if g is not None else {
            'total': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0,
            'requests': 0, 'hit_rate': 0.0, 'alpha': 0.12}
    return out


def stats_self_test():
    # 命中率（含除零）
    rows = [{'ts': 1786268700000, 'total': 20656, 'input': 60, 'output': 102,
             'cache_read': 20480, 'cache_write': 0}]
    g = list(aggregate_days(rows).values())[0]
    assert abs(g['hit_rate'] - 20480 / 20540) < 1e-4, g
    g2 = list(aggregate_days([{'ts': 1786268700000, 'total': 5, 'input': 5, 'output': 0,
                               'cache_read': 0, 'cache_write': 0}]).values())[0]
    assert g2['hit_rate'] == 0.0, g2
    # alpha 渐变：0 < 低 < 高 = 1.0，total=0 → 0.12
    dm = add_alpha({'a': {'total': 0}, 'b': {'total': 1000}, 'c': {'total': 500}})
    assert dm['b']['alpha'] == 0.85 and dm['a']['alpha'] == 0.12, dm
    assert dm['a']['alpha'] < dm['c']['alpha'] < dm['b']['alpha'], dm
    # 全量重扫回归：fill_days 90 项、键升序、最后一项是今天；空数据经全量流水线也产 90 天
    filled = fill_days({})
    assert len(filled) == STATS_DAYS, len(filled)
    keys = list(filled)
    assert keys == sorted(keys), keys
    assert keys[-1] == datetime.date.today().isoformat(), keys[-1]
    stats_cache['data'] = add_alpha(fill_days(aggregate_days([]), STATS_DAYS))
    assert len(stats_cache['data']) == STATS_DAYS, len(stats_cache['data'])
    # 真实库冒烟（库不存在则跳过）
    try:
        days = aggregate_days(read_daily_usage())
        filled = fill_days(days)
        nonempty = [k for k, v in filled.items() if v['total']]
        print(f'真实库: 有数据天数={len(nonempty)} 窗口={len(filled)}')
        for k in nonempty[-3:]:
            print(' ', k, filled[k])
    except Exception as e:
        print('真实库冒烟跳过:', e)
    print('stats_self_test OK')


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
        pystray.MenuItem('显示用量统计', lambda item: show_stats()),
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
    for w in all_windows():
        try:
            w.destroy()
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

    # 统计窗口按需创建，创建后样式在 _ensure_stats_window 里应用；这里无需等待
    def _style_stats():
        if stats_window is None:
            return
        for _ in range(200):
            if stats_window.native is not None:
                break
            time.sleep(0.1)
        apply_click_through(state.click_through)
        apply_topmost(state.topmost)
        hide_from_taskbar()
    threading.Thread(target=_style_stats, daemon=True).start()

    threading.Thread(target=fetch_loop, daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()
    threading.Thread(target=tray_thread, daemon=True).start()


def main():
    global window, stats_window
    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
        html = f.read()
    # 把保存的主题设置以 <style> 覆盖 :root 变量注入，渲染前立即生效，避免默认色闪烁
    cfg = load_config()
    settings = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in SETTING_KEYS}

    r, g, b = _hex_rgb(settings['bg_color'])
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
        js_api=Api('main'),
        width=460, height=360,
        # 置顶/穿透交给 Win32 手动管理；拖拽用 WM_NCLBUTTONDOWN 方案，不用 easy_drag
        frameless=True, transparent=True, on_top=False, easy_drag=False,
        background_color='#10131c',
    )

    def _on_closing():
        # 窗口真正销毁前记录最终位置 + 置顶/穿透状态（覆盖节流/未切换时未落盘的部分）
        save_on_exit()
    window.events.closing += _on_closing

    # 统计窗口按需创建（_ensure_stats_window）：启动时不创建，避免 hidden WebView2 窗口
    # 在加载页面执行 ExecuteScriptAsync 时永久阻塞 UI 线程导致启动未响应
    stats_window = None

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
