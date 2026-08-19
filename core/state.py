# -*- coding: utf-8 -*-
"""共享可变状态：窗口引用、数据缓存、事件、锁、托盘图标。

所有模块从这里读写全局状态，避免 main 单文件时代"到处直接碰全局变量"的耦合。
依赖：无（纯状态，唯一可被所有模块反向依赖的地基）。
用法：import core.state as state  →  state.topmost / state.cache / state.push_state()
"""
import json
import threading

# ---- 窗口引用（由对应模块创建后赋值）----
window = None        # 主窗口引用（core/main.py）
stats_window = None  # 用量统计窗口引用（core/api.py 按需创建）
icon = None          # 托盘图标对象（core/tray.py）

# ---- 运行时状态（置顶/穿透，启动时从 config 恢复）----
topmost = True
click_through = True

# ---- 数据缓存 ----
cache = {
    'usage': None, 'last_refresh': None, 'error': None, 'latency_ms': None,
    'plan_name': 'OpenCode Go',
    'settings': {},  # 首次 fetch_loop 跑起来前为空；main 启动时用 DEFAULT_CONFIG 初始化
}
stats_cache = {'data': {}, 'error': None, 'last_refresh': None, 'months': None,
               'month_data': None}

lock = threading.Lock()
STOP = threading.Event()
WAKE = threading.Event()  # 保存 API Key 后唤醒 fetch_loop 立即拉取
_pos_saved = [0.0]        # 拖拽期间主窗口位置写入 config 的节流时间戳
_stats_pos_saved = [0.0]  # 用量统计窗口位置的节流时间戳


def push_state():
    # 把置顶/穿透状态推给前端（托盘切换、配置面板临时关穿透后恢复等）
    try:
        window.evaluate_js(
            "window.applyState({topmost:%s,click_through:%s})"
            % (json.dumps(bool(topmost)), json.dumps(bool(click_through)))
        )
    except Exception:
        pass
