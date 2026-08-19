# -*- coding: utf-8 -*-
"""托盘图标与菜单。"""
import pystray
from PIL import Image, ImageDraw

import core.api as api
import core.config as config
import core.state as state
import core.win32 as win32


def make_icon():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=(30, 36, 52, 255), outline=(96, 140, 255, 255), width=3)
    d.arc([14, 14, 50, 50], start=-90, end=110, fill=(96, 140, 255, 255), width=5)
    return img


def tray_thread():
    menu = pystray.Menu(
        pystray.MenuItem('显示窗口', lambda item: win32.show_window()),
        pystray.MenuItem('显示用量统计', lambda item: api.show_stats()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda item: '置顶' + ('  ✓' if state.topmost else ''),
            lambda item: (win32.apply_topmost(not state.topmost), state.push_state(), config.save_toggles()),
            checked=lambda item: state.topmost,
        ),
        pystray.MenuItem(
            lambda item: '鼠标穿透' + ('  ✓' if state.click_through else ''),
            lambda item: (win32.apply_click_through(not state.click_through), state.push_state(), config.save_toggles()),
            checked=lambda item: state.click_through,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出', lambda item: api.stop_all()),
    )
    state.icon = pystray.Icon('opencode-monitor', make_icon(), 'OpencodeMonitor', menu)
    state.icon.run()
