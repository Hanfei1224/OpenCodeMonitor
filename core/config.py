# -*- coding: utf-8 -*-
"""配置：路径、默认值、主题键、位置/开关持久化。"""
import json
import os
import sys
import time

import core.state as state

# 冻结(打包)模式：config.json 放 exe 旁供用户编辑；index.html 从打包资源目录读
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, '_MEIPASS', APP_DIR)
else:
    # dev：本文件在 core/ 下，项目根是其父目录
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RES_DIR = APP_DIR
CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
INDEX_FILE = os.path.join(RES_DIR, 'index.html')

DEFAULT_CONFIG = {
    "api_key": "", "plan_name": "OpenCode Go", "refresh_seconds": 60,
    "bg_color": "#1e2330", "bg_opacity": 1.0, "accent_color": "#5b9bff",
    "window_x": None, "window_y": None,  # 上次退出的窗口位置，下次启动恢复
    "stats_window_x": None, "stats_window_y": None,  # 用量统计窗口位置，下次打开恢复
    "topmost": True, "click_through": True,  # 置顶/穿透状态，下次启动恢复
}
SETTING_KEYS = ('bg_color', 'bg_opacity', 'accent_color')


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


def _hex_rgb(h):
    h = (h or '').lstrip('#')
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (30, 35, 48)


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
    _save_pos(x, y, 'window_x', 'window_y', state._pos_saved, force)


def save_stats_pos(x, y, force=False):
    _save_pos(x, y, 'stats_window_x', 'stats_window_y', state._stats_pos_saved, force)


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
