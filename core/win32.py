# -*- coding: utf-8 -*-
"""Win32 原生窗口操作：句柄/exstyle/置顶/穿透/任务栏隐藏/位置保存。

依赖：core.state（窗口引用）、core.config（位置持久化）。
"""
import ctypes

import clr  # pythonnet：把 WinForms 操作 marshal 到 UI 线程
from System import Action

import core.config as config
import core.state as state

# ---- Win32 常量 ----
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


def all_windows():
    ws = [state.window]
    if state.stats_window is not None:
        ws.append(state.stats_window)
    return ws


def get_hwnd(target=None):
    try:
        w = target if target is not None else state.window
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
            ex |= WS_EX_TRANSPARENT
        else:
            ex &= ~WS_EX_TRANSPARENT
        # 必须保留 LAYERED 维持透明合成深色外观，否则半透明 HTML 直接透出桌面，
        # 浅色桌面下小部件变白（"白色覆盖"）
        ex |= WS_EX_LAYERED
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


def show_window():
    try:
        state.window.show()
    except Exception:
        pass
    hide_from_taskbar()  # show/hide 时序可能复位 exstyle，重挂工具窗口位


def save_on_exit():
    # 退出前记录主窗口与用量统计窗口位置。置顶/穿透不在此落盘（避免 API Key 面板临时关穿透被误存），只在托盘切换时保存
    hwnd = get_hwnd()
    if hwnd:
        rc = RECT()
        if _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            config.save_window_pos(rc.left, rc.top, force=True)
    shwnd = get_hwnd(state.stats_window) if state.stats_window is not None else None
    if shwnd:
        rc = RECT()
        if _user32.GetWindowRect(shwnd, ctypes.byref(rc)):
            config.save_stats_pos(rc.left, rc.top, force=True)
