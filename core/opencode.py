# -*- coding: utf-8 -*-
"""OpenCode Go 用量拉取：官方 /zen/go/v1/usage 的认证与轮询循环。"""
import json
import time
import urllib.request

import core.config as config
import core.state as state

USAGE_URL = 'https://opencode.ai/zen/go/v1/usage'


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
    while not state.STOP.is_set():
        cfg = config.load_config()
        key = (cfg.get('api_key') or '').strip()
        with state.lock:
            state.cache['plan_name'] = cfg.get('plan_name') or config.DEFAULT_CONFIG['plan_name']
            state.cache['settings'] = {k: cfg.get(k, config.DEFAULT_CONFIG[k]) for k in config.SETTING_KEYS}
        if not key:
            with state.lock:
                state.cache['usage'] = None
                state.cache['last_refresh'] = None
                state.cache['latency_ms'] = None
                state.cache['error'] = 'config_missing'
        else:
            try:
                data, latency = fetch_usage(key)
                with state.lock:
                    state.cache['usage'] = data.get('usage')
                    state.cache['last_refresh'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    state.cache['latency_ms'] = latency
                    state.cache['error'] = None
            except Exception as exc:
                msg = str(exc)
                if any(t in msg for t in ('401', '403', 'AuthError', 'Invalid API key', 'Missing API key')):
                    msg = 'API Key 无效'
                with state.lock:
                    state.cache['error'] = msg
                    state.cache['latency_ms'] = None
        # 等待下一轮：被 WAKE.set() 唤醒则立即重取（如刚保存了 API Key）
        state.WAKE.wait(int(cfg.get('refresh_seconds') or 60))
        state.WAKE.clear()
