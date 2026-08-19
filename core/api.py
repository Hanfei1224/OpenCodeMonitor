# -*- coding: utf-8 -*-
"""pywebview JS 桥（Api）+ 窗口动作层：用量统计窗口生命周期、退出。

依赖：core.state/config/win32/stats。
"""
import ctypes
import datetime
import json
import os
import time

import webview

import core.config as config
import core.state as state
import core.stats as stats
import core.win32 as win32


# ---- 用量统计窗口生命周期 ----
def _ensure_stats_window():
    # 按需创建统计窗口。pywebview 在非主线程调用 create_window 且 start 已执行时会
    # 立即创建并显示窗口（webview/__init__.py:418）。启动时预建 hidden 的 WebView2 窗口
    # 会卡死（ExecuteScriptAsync 在 UI 线程永久阻塞），故改为点击才创建。
    if state.stats_window is not None:
        return state.stats_window
    try:
        with open(os.path.join(config.RES_DIR, 'stats.html'), 'r', encoding='utf-8') as f:
            stats_html = f.read()
        # 注入保存的主题配色：<script> 给 JS 提供 accent/bg，<style> 覆盖默认 body 底色，
        # 首帧即用户设置色，避免先默认色再变强调色的闪烁
        cfg = config.load_config()
        settings = {k: cfg.get(k, config.DEFAULT_CONFIG[k]) for k in config.SETTING_KEYS}
        r, g, b = config._hex_rgb(settings['bg_color'])
        body_style = '<style>body{background:rgba(%d,%d,%d,1)}</style>' % (r, g, b)
        stats_html = stats_html.replace(
            '</head>',
            ('<script>window.__STATS_THEME__={accent_color:"%s",bg_color:"%s"}</script>'
             + body_style + '</head>')
            % (settings['accent_color'], settings['bg_color']))
        # 恢复到上次退出位置。config 存物理像素(GetWindowRect)；create_window 的 x/y 是
        # 逻辑/DPI 单位(实测×系统 DPI/96 缩放)，故先算在屏校验、再换算成逻辑坐标传入
        sx, sy = cfg.get('stats_window_x'), cfg.get('stats_window_y')
        x = y = None
        if sx is not None and sy is not None:
            try:
                scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
            except Exception:
                scale = 1.0
            if win32._on_screen(int(sx), int(sy), int(round(400 * scale)), int(round(450 * scale))):
                x, y = int(round(sx / scale)), int(round(sy / scale))
        w = webview.create_window(
            '用量统计', html=stats_html, js_api=Api('stats'),
            width=400, height=450, x=x, y=y, frameless=True, transparent=True,
            on_top=False, easy_drag=False, background_color='#10131c')
        state.stats_window = w
        # 立即套用当前置顶/穿透状态
        win32.apply_click_through(state.click_through)
        win32.apply_topmost(state.topmost)
        win32.hide_from_taskbar()
    except Exception:
        state.stats_window = None  # stats.html 缺失等：静默，下次点击重试
    return state.stats_window


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
    win32.hide_from_taskbar()  # show/hide 时序可能复位 exstyle，重挂工具窗口位


def stop_all():
    state.STOP.set()
    win32.save_on_exit()  # 退出前记录窗口位置（置顶/穿透在托盘切换时已落盘）
    if state.icon is not None:
        try:
            state.icon.stop()
        except Exception:
            pass
    for w in win32.all_windows():
        try:
            w.destroy()
        except Exception:
            pass


# ---- JS bridge ----
class Api:
    def __init__(self, win_id='main'):
        self._win_id = win_id
        self._drag = None

    def _win(self):
        return state.window if self._win_id == 'main' else state.stats_window

    def get_status(self):
        with state.lock:
            return {
                'usage': state.cache['usage'],
                'last_refresh': state.cache['last_refresh'],
                'latency_ms': state.cache['latency_ms'],
                'error': state.cache['error'],
                'plan_name': state.cache['plan_name'],
                'topmost': state.topmost,
                'click_through': state.click_through,
                'settings': dict(state.cache['settings']),
                'today_stats': stats._today_stats(),
                'refresh_seconds': int(config.load_config().get('refresh_seconds') or 60),
            }

    def save_settings(self, settings):
        cfg = config.load_config()
        for k in config.SETTING_KEYS:
            if k in settings:
                cfg[k] = settings[k]
        try:
            with open(config.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            with state.lock:
                state.cache['settings'] = {k: cfg[k] for k in config.SETTING_KEYS}
        except OSError:
            pass
        return True

    def save_api_key(self, key):
        cfg = config.load_config()
        cfg['api_key'] = (key or '').strip()
        try:
            with open(config.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        state.WAKE.set()  # 唤醒 fetch_loop 立即拉取，无需等下一个刷新周期
        return True

    def set_click_through(self, on):
        # 配置面板显示时临时关闭穿透，否则输入框无法点击
        win32.apply_click_through(bool(on))
        state.push_state()
        return True

    def toggle_topmost(self):
        # 主窗口置顶按钮点击：切换并持久化
        win32.apply_topmost(not state.topmost)
        state.push_state()
        config.save_toggles()
        return state.topmost

    def toggle_click_through(self):
        # 主窗口穿透按钮点击：切换并持久化
        win32.apply_click_through(not state.click_through)
        state.push_state()
        config.save_toggles()
        return state.click_through

    def start_drag(self):
        # 记录窗口原始位置 + 光标起始位置，move_window 按增量移动（DPI 虚拟化下坐标单位不同，需乘缩放系数）
        hwnd = win32.get_hwnd(self._win())
        if not hwnd:
            return True
        try:
            rc = win32.RECT()
            if not win32._user32.GetWindowRect(hwnd, ctypes.byref(rc)):
                return True
            pt = win32.POINT()
            win32._user32.GetCursorPos(ctypes.byref(pt))
            self._drag = {'ox': rc.left, 'oy': rc.top, 'cx': pt.x, 'cy': pt.y}
        except Exception:
            pass
        return True

    def move_window(self, dx, dy):
        hwnd = win32.get_hwnd(self._win())
        if not hwnd or not self._drag:
            return True
        try:
            pt = win32.POINT()
            win32._user32.GetCursorPos(ctypes.byref(pt))
            # 进程已被 WebView2 设为 DPI-aware，GetCursorPos/SetWindowPos 均为物理像素，直接 1:1 增量
            nx = self._drag['ox'] + (pt.x - self._drag['cx'])
            ny = self._drag['oy'] + (pt.y - self._drag['cy'])
            win32._user32.SetWindowPos(hwnd, 0, nx, ny, 0, 0,
                                       win32.SWP_NOSIZE | win32.SWP_NOZORDER | win32.SWP_NOACTIVATE)
            if self._win_id == 'main':
                config.save_window_pos(nx, ny)
            else:
                config.save_stats_pos(nx, ny)
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
        with state.lock:
            return dict(state.stats_cache)

    def set_refresh_seconds(self, seconds):
        # 只接受预设刷新间隔，非法值拒绝（防注入）。写 config 后唤醒采集循环立即按新间隔重排
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return False
        if seconds not in (5, 10, 30, 60):
            return False
        cfg = config.load_config()
        cfg['refresh_seconds'] = seconds
        try:
            with open(config.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError:
            return False
        state.WAKE.set()  # 唤醒 fetch_loop / stats_loop 立即用新间隔重排
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
            view = stats.build_month_view(y, m)
            view['available_months'] = stats.available_months()
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
