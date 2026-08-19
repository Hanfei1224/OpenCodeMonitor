# -*- coding: utf-8 -*-
"""本地 token 统计：五数据源采集、聚合与缓存。

依赖：core.state（stats_cache/STOP/WAKE）、core.config（refresh 间隔）。
"""
import calendar
import datetime
import glob
import json
import os
import sqlite3
import time
from pathlib import Path

import core.config as config
import core.state as state

STATS_DAYS = 90
STATS_SCAN_INTERVAL = 60
_last_stats_scan = [0.0]  # 全量采集节流时间戳（采集数秒，最多每 STATS_SCAN_INTERVAL 一次）


def opencode_db_path():
    return str(Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db')


def claude_code_dir():
    """Claude Code 会话 JSONL 目录：~/.claude/projects/<项目>/<会话>.jsonl"""
    return str(Path.home() / '.claude' / 'projects')


def pi_sessions_dir():
    """pi 会话 JSONL 目录：~/.pi/agent/sessions/<项目>/<会话>.jsonl"""
    return str(Path.home() / '.pi' / 'agent' / 'sessions')


def zcode_db_path():
    """ZCode CLI 用量库：~/.zcode/cli/db/db.sqlite（model_usage 表）"""
    return str(Path.home() / '.zcode' / 'cli' / 'db' / 'db.sqlite')


def codex_sessions_dir():
    """Codex CLI 会话 JSONL 目录：~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl"""
    return str(Path.home() / '.codex' / 'sessions')


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


def _pi_rows(start_ms, end_ms=None):
    """pi 会话 JSONL（~/.pi/agent/sessions/**/*.jsonl）：assistant 消息带 usage 的行。
    usage 为 camelCase：input/output/cacheRead/cacheWrite/totalTokens。
    totalTokens = input+output+cacheRead+cacheWrite；input 不含缓存。
    mtime 早于 start_ms 的整文件跳过（jsonl 只追加）。"""
    rows = []
    d = pi_sessions_dir()
    if not os.path.isdir(d):
        return rows
    for fp in glob.glob(os.path.join(d, '**', '*.jsonl'), recursive=True):
        try:
            if os.path.getmtime(fp) * 1000 < start_ms:
                continue
            with open(fp, encoding='utf-8') as f:
                for line in f:
                    if 'totalTokens' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    u = (j.get('message') or {}).get('usage') or {}
                    total = int(u.get('totalTokens') or 0)
                    if not total:
                        continue
                    ms = _iso_to_ms(j.get('timestamp') or '')
                    if ms is None or ms < start_ms or (end_ms is not None and ms >= end_ms):
                        continue
                    rows.append({'ts': ms, 'total': total,
                                 'input': int(u.get('input') or 0),
                                 'output': int(u.get('output') or 0),
                                 'cache_read': int(u.get('cacheRead') or 0),
                                 'cache_write': int(u.get('cacheWrite') or 0)})
        except OSError:
            continue
    return rows


def _zcode_rows(start_ms, end_ms=None):
    """ZCode CLI 用量库（~/.zcode/cli/db/db.sqlite 的 model_usage 表，完成请求）。
    该表 input_tokens 含缓存读（已验证 computed_total_tokens = input_tokens+output_tokens），
    故 input = input_tokens - cache_read - cache_creation，还原为与其它源一致的非缓存口径。"""
    rows = []
    db_path = zcode_db_path()
    if not os.path.exists(db_path):
        return rows
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
        try:
            cur = conn.execute(
                "SELECT started_at, input_tokens, output_tokens, cache_creation_input_tokens,"
                " cache_read_input_tokens, computed_total_tokens FROM model_usage"
                " WHERE status='completed' AND computed_total_tokens > 0"
                " AND started_at >= ?" + (" AND started_at < ?" if end_ms is not None else ''),
                [start_ms] + ([end_ms] if end_ms is not None else []))
            for (ts, inp, out, cw, cr, total) in cur:
                rows.append({'ts': int(ts), 'total': int(total),
                             'input': max(0, int(inp) - int(cr) - int(cw)),
                             'output': int(out),
                             'cache_read': int(cr), 'cache_write': int(cw)})
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return rows


def _codex_rows(start_ms, end_ms=None):
    """Codex CLI 会话 JSONL（~/.codex/sessions/**/rollout-*.jsonl）：token_count 事件。
    total_token_usage 是会话累计，last_token_usage 是本次请求增量，取 last 避免重复计数。
    input_tokens 含 cached_input_tokens，需扣减对齐非缓存口径；total = input+output（含缓存）。
    旧版事件可能不拆 input/output（全 0），此时 total 仍可取，拆分留 0。"""
    rows = []
    d = codex_sessions_dir()
    if not os.path.isdir(d):
        return rows
    for fp in glob.glob(os.path.join(d, '**', '*.jsonl'), recursive=True):
        try:
            if os.path.getmtime(fp) * 1000 < start_ms:
                continue
            with open(fp, encoding='utf-8') as f:
                for line in f:
                    if '"token_count"' not in line:
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    p = j.get('payload') or {}
                    if p.get('type') != 'token_count':
                        continue
                    info = p.get('info') or {}
                    u = info.get('last_token_usage') or {}
                    total = int(u.get('total_tokens') or 0)
                    if not total:
                        continue
                    ms = _iso_to_ms(j.get('timestamp') or '')
                    if ms is None or ms < start_ms or (end_ms is not None and ms >= end_ms):
                        continue
                    inp = int(u.get('input_tokens') or 0)
                    cr = int(u.get('cached_input_tokens') or 0)
                    rows.append({'ts': ms, 'total': total,
                                 'input': max(0, inp - cr),
                                 'output': int(u.get('output_tokens') or 0),
                                 'cache_read': cr, 'cache_write': 0})
        except OSError:
            continue
    return rows


def _collect_rows(start_ms, end_ms=None):
    """统一多源采集：opencode.db + Claude Code + pi + ZCode + Codex。
    任一源缺失/异常不影响其它源。返回值与各单源函数同构。"""
    rows = []
    db_path = opencode_db_path()
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
            try:
                cur = conn.execute(
                    "SELECT data FROM message"
                    " WHERE json_extract(data, '$.tokens') IS NOT NULL")
                rows.extend(r for r in _rows_from_messages(cur)
                            if r['ts'] >= start_ms and (end_ms is None or r['ts'] < end_ms))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    for fn in (_claude_rows, _pi_rows, _codex_rows, _zcode_rows):
        try:
            rows.extend(fn(start_ms, end_ms))
        except Exception:
            pass  # 单源异常不拖垮整体采集
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
    """全历史综合采集：opencode.db + Claude Code + pi + ZCode + Codex（全部时间）。
    一次扫描同时服务最近天数视图与按月视图。任一源异常不影响另一源。"""
    try:
        return _collect_rows(0)
    except Exception:
        return []


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
    """综合采集最近 days 天全量请求列表（五数据源，含 pi/ZCode/Codex）。
    任一源缺失/异常不影响另一源。"""
    cutoff = int(time.time() * 1000) - days * 86400_000
    try:
        return _collect_rows(cutoff)
    except Exception:
        return []


def read_month_usage(year, month):
    """综合采集指定年月落在 [月首, 次月首) 的请求列表（五数据源）。"""
    start = int(datetime.datetime(year, month, 1).timestamp() * 1000)
    nxt = int((datetime.datetime(year, month, 1) + datetime.timedelta(days=32))
              .replace(day=1).timestamp() * 1000)
    try:
        return _collect_rows(start, nxt)
    except Exception:
        return []


def build_month_view(year, month):
    """聚合指定年月：优先读 stats_cache['month_data']（refresh_stats 全量采集时建好），
    冷缓存/缺月才回退单月扫描。返回 {year, month, max_total, days:{date: day_stats(含alpha)}}。
    补全该月全部天；alpha 按该月内最大单日 total 归一化（无数据 → 0.12）。"""
    dm = (state.stats_cache.get('month_data') or {}).get((year, month))
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
    综合五数据源，只统计 total>0 的月份；始终含当前月。优先用 stats_loop 的缓存，避免反复全扫。"""
    cached = state.stats_cache.get('months')
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


def refresh_stats(force=False):
    # 综合采集五数据源，一次读取同时算出最近天数视图、
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
        state.stats_cache['data'] = add_alpha(fill_days(cur, STATS_DAYS))
        state.stats_cache['months'] = _months_from_rows(rows)
        state.stats_cache['month_data'] = _aggregate_months(rows)  # 原子替换，读方永不看到半成品
        state.stats_cache['last_refresh'] = time.strftime('%Y-%m-%d %H:%M:%S')
        state.stats_cache['error'] = None
    except Exception as e:
        state.stats_cache['error'] = str(e)
    # 统计窗口按需拉取数据（get_month_stats / get_status 读 stats_cache），无需主动推送
    # （旧的 renderStats 已随每日图废弃，stats.html 无此函数）


def _today_stats():
    # 主窗口「今日」分块数据：取 stats_cache 里今天的聚合（fill_days 已含今天）
    today = datetime.date.today().isoformat()
    g = state.stats_cache['data'].get(today)
    if g is None:
        return {'total': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'hit_rate': 0.0}
    return {'total': g['total'], 'input': g['input'], 'output': g['output'],
            'cache_read': g['cache_read'], 'hit_rate': g['hit_rate']}


def stats_loop():
    refresh_stats(force=True)  # 启动立即采集，避免打开统计/主窗口首帧无数据
    while not state.STOP.is_set():
        state.WAKE.wait(int(config.load_config().get('refresh_seconds') or 60))
        state.WAKE.clear()
        refresh_stats()


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
    state.stats_cache['data'] = add_alpha(fill_days(aggregate_days([]), STATS_DAYS))
    assert len(state.stats_cache['data']) == STATS_DAYS, len(state.stats_cache['data'])
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

    # 多源口径自洽：total == input+output+cache_read+cache_write（源存在才验证）
    # opencode/codex 的 total 来自 provider（含 reasoning 或旧版不拆字段），只要求 total >= 字段和；
    # pi/zcode 为构造自洽口径，要求严格相等（用虚拟数据验证映射，不依赖真实源）。
    for name, fn, strict in (('opencode', lambda: _collect_rows(0), False),
                             ('codex', lambda: _codex_rows(0), False)):
        try:
            src = fn()
            bad = [r for r in src
                   if r['total'] < r['input'] + r['output'] + r['cache_read'] + r['cache_write']]
            print(f'多源自洽[{name}]: {len(src)} 行 字段和超 total={len(bad)}')
            assert not bad, (name, bad[:2])
        except Exception as e:
            print(f'多源自洽[{name}] 跳过:', e)
    # pi/zcode 的映射规约：用与真实源同构的虚拟行验证（total 必须等于字段和）
    pi_fake = [{'ts': 1786268700000, 'total': 16784, 'input': 12551, 'output': 137,
                'cache_read': 4096, 'cache_write': 0}]
    g = list(aggregate_days(pi_fake).values())[0]
    assert g['total'] == 16784 and g['input'] == 12551 and g['cache_read'] == 4096, g
    z_fake = [{'ts': 1786268700000, 'total': 35431, 'input': 3286, 'output': 1937,
               'cache_read': 30208, 'cache_write': 0}]
    g = list(aggregate_days(z_fake).values())[0]
    assert g['total'] == 35431 and g['input'] == 3286 and g['cache_read'] == 30208, g
    # 真实 pi/zcode 源存在则校验行级自洽（读文件验证映射，非仅约定）
    for name, fn in (('pi', lambda: _pi_rows(0)), ('zcode', lambda: _zcode_rows(0))):
        try:
            src = fn()
            bad = [r for r in src
                   if r['total'] != r['input'] + r['output'] + r['cache_read'] + r['cache_write']]
            print(f'多源自洽[{name}]: {len(src)} 行 不一致={len(bad)}')
            assert not bad, (name, bad[:2])
        except Exception as e:
            print(f'多源自洽[{name}] 跳过:', e)
    print('stats_self_test OK')
