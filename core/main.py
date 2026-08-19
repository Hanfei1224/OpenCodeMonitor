# -*- coding: utf-8 -*-
"""入口：窗口创建、主题注入、boot 时序、线程装配。

运行：项目根目录 `python -m core.main`（或 python core/main.py）。
"""
import ctypes
import os
import sys
import threading
import time

import webview

# dev 模式下 `python core/main.py` 需要项目根在 sys.path 才能 `import core.*`
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.api as api
import core.config as config
import core.opencode as opencode
import core.state as state
import core.stats as stats
import core.tray as tray
import core.win32 as win32


def on_start():
    # native 由 GUI 线程在创建窗口时赋值，本回调线程可能先跑，轮询等待
    for _ in range(200):
        if state.window.native is not None:
            break
        time.sleep(0.1)
    cfg = config.load_config()
    # 从 config 恢复置顶/穿透状态
    state.topmost = bool(cfg.get('topmost', True))
    state.click_through = bool(cfg.get('click_through', True))
    win32.apply_click_through(state.click_through)
    win32.apply_topmost(state.topmost)
    win32.hide_from_taskbar()

    # 恢复到上次退出位置（与拖拽同一套物理像素坐标）
    sx, sy = cfg.get('window_x'), cfg.get('window_y')
    hwnd = win32.get_hwnd()
    if hwnd and sx is not None and sy is not None:
        rc = win32.RECT()
        if win32._user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            w, h = rc.right - rc.left, rc.bottom - rc.top
            if win32._on_screen(int(sx), int(sy), w, h):
                win32._user32.SetWindowPos(hwnd, 0, int(sx), int(sy), 0, 0,
                                           win32.SWP_NOSIZE | win32.SWP_NOZORDER | win32.SWP_NOACTIVATE)

    # 页面真正加载完成（窗口已稳定显示）后再重挂样式。冷启动 WebView2 初始化慢，固定 sleep
    # 可能在窗口真正显示前就重挂、随后又被覆盖；改等 loaded 事件，超时兜底。
    def _reauth():
        try:
            state.window.events.loaded.wait(20)
        except Exception:
            time.sleep(3)
        win32.apply_click_through(state.click_through)
        win32.apply_topmost(state.topmost)
        win32.hide_from_taskbar()
    threading.Thread(target=_reauth, daemon=True).start()

    # 统计窗口按需创建，创建后样式在 api._ensure_stats_window 里应用；这里无需等待
    def _style_stats():
        if state.stats_window is None:
            return
        for _ in range(200):
            if state.stats_window.native is not None:
                break
            time.sleep(0.1)
        win32.apply_click_through(state.click_through)
        win32.apply_topmost(state.topmost)
        win32.hide_from_taskbar()
    threading.Thread(target=_style_stats, daemon=True).start()

    threading.Thread(target=opencode.fetch_loop, daemon=True).start()
    threading.Thread(target=stats.stats_loop, daemon=True).start()
    threading.Thread(target=tray.tray_thread, daemon=True).start()


def main():
    with open(config.INDEX_FILE, 'r', encoding='utf-8') as f:
        html = f.read()
    # 把保存的主题设置以 <style> 覆盖 :root 变量注入，渲染前立即生效，避免默认色闪烁
    cfg = config.load_config()
    settings = {k: cfg.get(k, config.DEFAULT_CONFIG[k]) for k in config.SETTING_KEYS}
    # 首次 fetch_loop 跑起来前，get_status 需要默认 settings 供前端首帧渲染
    state.cache['settings'] = dict(settings)

    r, g, b = config._hex_rgb(settings['bg_color'])
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
        js_api=api.Api('main'),
        width=460, height=360,
        # 置顶/穿透交给 Win32 手动管理；拖拽用 WM_NCLBUTTONDOWN 方案，不用 easy_drag。
        frameless=True, transparent=True, on_top=False, easy_drag=False,
        background_color='#10131c',
    )
    state.window = window

    def _on_closing():
        # 窗口真正销毁前记录最终位置 + 置顶/穿透状态（覆盖节流/未切换时未落盘的部分）
        win32.save_on_exit()
    window.events.closing += _on_closing

    # 统计窗口按需创建（api._ensure_stats_window）：启动时不创建，避免 hidden WebView2 窗口
    # 在加载页面执行 ExecuteScriptAsync 时永久阻塞 UI 线程导致启动未响应
    state.stats_window = None

    webview.start(on_start, icon=os.path.join(config.RES_DIR, 'icon.ico'))
    api.stop_all()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        try:
            with open(os.path.join(config.APP_DIR, 'app.log'), 'w', encoding='utf-8') as f:
                f.write(traceback.format_exc())
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(0, '启动失败，详情见 app.log', 'OpencodeMonitor', 0x10)
        raise
