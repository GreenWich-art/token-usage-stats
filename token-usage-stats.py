#!/usr/bin/env python3
"""
token-usage-stats.py — AI 模型 Token 用量统计（数据源：Agent HUD 本地账本）

背景（2026-10-20 与用户共同确认的方案）：
  Agent HUD 把 WorkBuddy / Codex 等客户端的每次模型调用归一化记账到
  ~/Library/Application Support/Agent HUD Open/usage-ledger.sqlite
  （usage_event 逐事件：tokens_in/out/cache_read/cache_write/reasoning + 时间戳 + agent）。
  本工具只读该库，聚合成「按模型 / 按日 / 按模型×日」三维统计，产出：
    1) token-usage-report.html      自包含 HTML 报告（内联 SVG 图表，无外部依赖，离线可看）
    2) token-usage-by-model.csv     按模型汇总
    3) token-usage-daily.csv        按日汇总
    4) token-usage-model-daily.csv  模型×日 长表（透视分析用）

口径说明：
  - 只统计 contribution.counted = 1 的事件（与 HUD 展示口径一致）
  - 总量含 cache_read（与 HUD「Tokens over time」等视图一致）；报告内同时给出拆分
  - 历史深度 = HUD 开始记账的时间（当前最早 2026-10-07），随日常累积
  - agent 名形如 workbuddy-model:<id> / codex-model:<id>；模型标识沿用 HUD 归类

用法：
  python3 token-usage-stats.py                # 全量历史，输出到脚本旁 token-usage-reports/
  python3 token-usage-stats.py --days 7       # 仅最近 7 天
  python3 token-usage-stats.py --top 15       # 热力图显示前 15 个模型（默认 12）
  python3 token-usage-stats.py --out-dir DIR # 自定义输出目录
退出码：0 正常；1 运行错误；2 账本不存在
"""
import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = Path.home() / "Library/Application Support/Agent HUD Open/usage-ledger.sqlite"
DEFAULT_BILLING_REPORT = Path.home() / "Library/Application Support/Agent HUD Open/last-usage-report.json"
DEFAULT_OUT = SCRIPT_DIR / "token-usage-reports"
PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"
VERSION = "2.0.0"

CLIENT_NAMES = {"workbuddy": "WorkBuddy", "codex": "Codex", "claude": "Claude Code"}


# ---------------------------------------------------------------- 数据加载
# 口径实现已抽到 wb_token_semantics.py（单一事实来源）。
# 本文件不再自行解析用量，只把语义层的事件字段适配成历史字段名，
# 让下游 aggregate / 渲染代码零改动。这样 Python 报告、本地服务、
# 未来的浏览器端 JS 版共用同一套口径，并由 test_semantics.py 守住。

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import wb_token_semantics as sem  # noqa: E402


def _cutoff_ms(days):
    """days → 毫秒时间戳下限（None 表示不限）。"""
    return int((time.time() - days * 86400) * 1000) if days else None


def _adapt(events):
    """语义层事件 → 本文件历史使用的行字段名。"""
    for ev in events:
        yield {
            "timestamp_ms": ev["ts_ms"],
            "agent": ev["agent"],
            "tokens_in": ev["tin"],
            "tokens_out": ev["tout"],
            "cache_read": ev["cread"],
            "cache_write": ev["cwrite"],
            "reasoning": ev["reasoning"],
        }


def load_events(db_path: Path, days: int | None = None):
    """旧接口：直接走 HUD 账本（全部客户端），仅供 --source ledger 对照/诊断。"""
    if not db_path.exists():
        print(f"[!] 账本不存在: {db_path}", file=sys.stderr)
        sys.exit(2)
    return list(_adapt(sem.collect_ledger(db_path, _cutoff_ms(days))))


def load_transcript_events(days: int | None = None):
    """仅 WorkBuddy：直读原始会话（免疫工作目录改名）。"""
    return list(_adapt(sem.collect_transcripts(PROJECTS_DIR, _cutoff_ms(days))))


def gather_rows(db_path: Path, days: int | None = None, source: str = "auto"):
    """按数据源策略汇总逐事件行（语义见 wb_token_semantics.collect_*）。

    auto（默认）: WorkBuddy 走 transcripts；其余客户端（Codex/Claude/DeepSeek）走账本
    transcripts : 仅 transcripts（WorkBuddy only）
    ledger      : 全部走 HUD 账本（旧行为，仅用于对照/诊断）
    """
    since = _cutoff_ms(days)
    if source == "ledger":
        return load_events(db_path, days)
    if source == "transcripts":
        return load_transcript_events(days)
    if not db_path.exists():
        return load_transcript_events(days)
    return list(_adapt(sem.collect_auto(PROJECTS_DIR, db_path, since)))


def load_billing_kinds(report_path: Path) -> dict:
    """从 HUD 最近一次用量报告读取模型的计费方式归类（可选，缺失时降级）。

    consumers[] 中 id = '<client>-model:<id>'：
      无 meteredBy            → WorkBuddy 内置计量
      meteredBy.api           → 自有 API（vendor 为厂商）
      meteredBy.plan          → 订阅套餐
    """
    kinds = {}
    try:
        with open(report_path) as f:
            data = json.load(f)
        for c in data.get("consumers", []):
            cid = c.get("id", "")
            metered = c.get("meteredBy") or {}
            if "plan" in metered:
                kinds[cid] = "订阅套餐"
            elif "api" in metered:
                vendor = (metered["api"] or {}).get("vendor") or "自有 API"
                kinds[cid] = f"自有 API（{vendor}）"
            else:
                kinds[cid] = "内置计量"
    except Exception:
        pass
    return kinds


def split_agent(agent: str):
    """'workbuddy-model:step-5-preview' → ('workbuddy', 'step-5-preview')"""
    if "-model:" in agent:
        client, _, model = agent.partition("-model:")
        return client, model
    return "other", agent


# ---------------------------------------------------------------- 聚合

def aggregate(rows):
    by_model = defaultdict(lambda: dict(events=0, tin=0, tout=0, cread=0, cwrite=0, reasoning=0, days=set()))
    by_day = defaultdict(lambda: dict(events=0, tin=0, tout=0, cread=0, cwrite=0, reasoning=0))
    by_md = defaultdict(int)  # (date, agent) -> total
    dates = set()
    for r in rows:
        ts = r["timestamp_ms"] / 1000
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        client, model = split_agent(r["agent"])
        total = r["tokens_in"] + r["tokens_out"] + r["cache_read"] + r["cache_write"] + r["reasoning"]
        m = by_model[(client, model)]
        m["events"] += 1
        for k, col in (("tin", "tokens_in"), ("tout", "tokens_out"), ("cread", "cache_read"),
                       ("cwrite", "cache_write"), ("reasoning", "reasoning")):
            m[k] += r[col] or 0
        m["days"].add(day)
        d = by_day[day]
        d["events"] += 1
        for k, col in (("tin", "tokens_in"), ("tout", "tokens_out"), ("cread", "cache_read"),
                       ("cwrite", "cache_write"), ("reasoning", "reasoning")):
            d[k] += r[col] or 0
        by_md[(day, r["agent"])] += total
        dates.add(day)
    return by_model, by_day, by_md, sorted(dates)


# ---------------------------------------------------------------- 格式化

def fmt_compact(n: float) -> str:
    """大数字紧凑格式：亿 / 万。"""
    n = float(n)
    if n >= 1e8:
        return f"{n / 1e8:.2f} 亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f} 万"
    return f"{int(n):,}"


def fmt_int(n) -> str:
    return f"{int(n):,}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def nice_ceil(v: float) -> float:
    """把坐标轴上限取整到 1/2/5 × 10^k。"""
    if v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    base = 10 ** exp
    for m in (1, 2, 5, 10):
        if v <= m * base:
            return m * base
    return 10 * base


def lerp_color(c1, c2, t: float):
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(
        round(a + (b - a) * t) for a, b in zip(c1, c2)
    )


# ---------------------------------------------------------------- SVG 图表

DAY_W = 115  # 趋势图每天占用的横向宽度（视口约显示一周）


def svg_daily_area(daily_rows):
    """每日总 tokens 堆叠面积图（输入/输出/缓存读取 三段构成）。

    返回 (axis_svg, plot_svg, js_data)：
      - axis_svg : 固定不滚动的 y 轴层（62px 宽），横向滚动时始终可见
      - plot_svg : 可横向滚动的绘图区（每天 DAY_W px），页面加载后自动滚到最右端（最近一周）
      - js_data  : 悬停提示用的坐标与当日明细
    标点数值不在图上静态标注，改为悬停提示（避免拥挤）。
    """
    if not daily_rows:
        return "<p class='empty'>所选时间范围内没有数据</p>", "", []
    H, pt, ih = 300, 18, 252
    n = len(daily_rows)
    ymax = nice_ceil(max(sum(r[k] for k in ("tin", "tout", "cread", "cwrite", "reasoning")) for r in daily_rows) or 1)
    W = int(n * DAY_W + 14)

    def x(i):
        return DAY_W * i + DAY_W / 2

    def y(v):
        return pt + ih - ih * v / ymax

    # 固定 y 轴层
    ax = ["<svg class='yaxis' width='62' height='300' viewBox='0 0 62 300' aria-hidden='true'>"]
    for f in (0, 0.25, 0.5, 0.75, 1.0):
        gy = pt + ih - ih * f
        ax.append(f"<text x='54' y='{gy + 4:.1f}' class='axis' text-anchor='end'>{fmt_compact(ymax * f)}</text>")
    ax.append("</svg>")

    series = [("tin", "#3b82f6", "输入"), ("tout", "#f59e0b", "输出"), ("cread", "#10b981", "缓存读取")]
    parts = [f"<svg id='trend' width='{W}' height='300' viewBox='0 0 {W} 300' class='chartwide' role='img' aria-label='每日 token 消耗趋势'>"]
    for f in (0, 0.25, 0.5, 0.75, 1.0):
        gy = pt + ih - ih * f
        parts.append(f"<line x1='0' y1='{gy:.1f}' x2='{W}' y2='{gy:.1f}' class='grid'/>")
    cum = [0.0] * n
    for key, color, _label in series:
        pts_top, pts_bottom = [], []
        for i, r in enumerate(daily_rows):
            v0, v1 = cum[i], cum[i] + r[key]
            cum[i] = v1
            pts_top.append((x(i), y(v1)))
            pts_bottom.append((x(i), y(v0)))
        area = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts_top)
        area += " " + " ".join(f"{px:.1f},{py:.1f}" for px, py in reversed(pts_bottom))
        parts.append(f"<polygon points='{area}' fill='{color}' fill-opacity='0.28'/>")
        line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts_top)
        parts.append(f"<polyline points='{line}' fill='none' stroke='{color}' stroke-width='1.8'/>")
    js_data = []
    for i, r in enumerate(daily_rows):
        total = sum(r[k] for k in ("tin", "tout", "cread", "cwrite", "reasoning"))
        px, py = x(i), y(total)
        parts.append(f"<circle cx='{px:.1f}' cy='{py:.1f}' r='3' fill='#111827'/>")
        js_data.append({"d": r["date"], "px": round(px, 1), "py": round(py, 1),
                        "total": total, "tin": r["tin"], "tout": r["tout"], "cread": r["cread"]})
        # 日期标签：每天一个（每天占 115px，不会拥挤）
        parts.append(f"<text x='{px:.1f}' y='{H - 8}' class='axis' text-anchor='middle'>{r['date'][5:]}</text>")
    # 悬停热区（每列一整条）+ 参考线 + 高亮点
    for i in range(n):
        parts.append(f"<rect class='hov' data-i='{i}' x='{DAY_W * i}' y='{pt}' width='{DAY_W}' height='{ih}' fill='transparent'/>")
    parts.append(f"<line id='guide' x1='0' y1='{pt}' x2='0' y2='{pt + ih}' stroke='#9ca3af' stroke-dasharray='3,3' stroke-width='1' opacity='0'/>")
    parts.append("<circle id='hovdot' r='4.5' fill='#111827' stroke='#fff' stroke-width='1.5' opacity='0'/>")
    parts.append("</svg>")
    return "".join(ax), "".join(parts), js_data


def heatmap_html(model_day, models, dates, top_n):
    """模型 × 日期 热力图（HTML 表格版）。

    设计：模型名列 position:sticky 固定，色块与上方日期一起横向滚动；
    默认滚到最右（最近一周），向左滑看更早日期。色块 54×24px（比 SVG 版大一倍多）。
    """
    if not models or not dates:
        return "<p class='empty'>所选时间范围内没有数据</p>"
    models = models[:top_n]
    vals = [v for (d, a), v in model_day.items() if a in models and v > 0]
    vmax = max(vals) if vals else 1
    lo = math.log10(max(min(vals), 1)) if vals else 0
    hi = math.log10(max(vmax, 1))

    def color(v):
        if v <= 0:
            return "#f3f4f6"
        t = (math.log10(v) - lo) / (hi - lo) if hi > lo else 1.0
        return lerp_color((219, 234, 254), (30, 58, 138), t)

    head = "".join(f"<th>{d[5:]}</th>" for d in dates)
    rows = []
    for agent in models:
        _c, model = split_agent(agent)
        cells = []
        for d in dates:
            v = model_day.get((d, agent), 0)
            cells.append(
                f"<td class='cell'><i style='background:{color(v)}' "
                f"title='{d} {model}：{fmt_int(v)} token'></i></td>"
            )
        total = sum(model_day.get((d, agent), 0) for d in dates)
        rows.append(
            f"<tr><th class='hmlabel' title='{model}'>{model}</th>{''.join(cells)}"
            f"<td class='hmtotal'>{fmt_compact(total)}</td></tr>"
        )
    return (
        "<div class='hmwrap'><table class='hm'>"
        f"<thead><tr><th class='hmlabel'>模型</th>{head}<th class='hmtotal'>合计</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


# ---------------------------------------------------------------- HTML

TREND_SCRIPT = """
<script>
(function(){
  var DATA = __DATA__;
  var svg = document.getElementById('trend');
  // 默认滚到最右端：只显示最近一周，向左滑看更早
  var sc = svg && svg.closest('.scrollx');
  var hms = document.querySelectorAll('.hmwrap');
  function goEnd(){
    if (sc) sc.scrollLeft = sc.scrollWidth;
    for (var h = 0; h < hms.length; h++) hms[h].scrollLeft = hms[h].scrollWidth;
  }
  goEnd();
  window.addEventListener('load', goEnd);  // Safari 布局时序兜底
  if (!svg || !DATA.length) return;
  var tip = document.getElementById('tip');
  var guide = document.getElementById('guide');
  var dot = document.getElementById('hovdot');
  var view = svg.closest('.chartview');
  function fmt(v){
    if (v >= 1e8) return (v/1e8).toFixed(2) + ' 亿';
    if (v >= 1e4) return (v/1e4).toFixed(1) + ' 万';
    return String(Math.round(v));
  }
  function show(i){
    var d = DATA[i];
    guide.setAttribute('x1', d.px); guide.setAttribute('x2', d.px);
    guide.setAttribute('opacity', '1');
    dot.setAttribute('cx', d.px); dot.setAttribute('cy', d.py);
    dot.setAttribute('opacity', '1');
    tip.innerHTML =
      '<div class="tip-d">' + d.d + '</div>' +
      '<div class="tip-r"><i style="background:#111827"></i>Token 消耗总量：<b>' + fmt(d.total) + '</b></div>' +
      '<div class="tip-r"><i style="background:#3b82f6"></i>输入：<b>' + fmt(d.tin) + '</b></div>' +
      '<div class="tip-r"><i style="background:#f59e0b"></i>输出：<b>' + fmt(d.tout) + '</b></div>' +
      '<div class="tip-r"><i style="background:#10b981"></i>缓存读取：<b>' + fmt(d.cread) + '</b></div>';
    tip.style.opacity = '1';
    // SVG 按 1:1 像素渲染，d.px 即屏幕像素；再换算成相对可视容器的位置（含滚动位移）
    var pr = svg.getBoundingClientRect();
    var cv = view.getBoundingClientRect();
    var sx = pr.left + d.px - cv.left;
    var tw = tip.offsetWidth;
    var left = Math.min(Math.max(sx - tw / 2, 8), cv.width - tw - 8);
    tip.style.left = left + 'px';
  }
  function hide(){
    tip.style.opacity = '0';
    guide.setAttribute('opacity', '0');
    dot.setAttribute('opacity', '0');
  }
  var rects = svg.querySelectorAll('.hov');
  for (var k = 0; k < rects.length; k++){
    rects[k].addEventListener('mouseenter', function(){ show(+this.getAttribute('data-i')); });
    rects[k].addEventListener('mouseleave', hide);
  }
})();
</script>
"""


LIVE_URL = "http://127.0.0.1:8791/"

BANNER_SCRIPT = """
<script>
(function(){
  var LIVE = "__LIVE_URL__";
  var b = document.createElement('div');
  if (location.protocol === 'file:') {
    b.textContent = '静态快照 · 数据截至生成时刻 — 实时版（刷新即更新）：' + LIVE;
    b.style.cssText = 'position:fixed;left:0;right:0;bottom:0;background:#fef3c7;color:#92400e;' +
      'font:12px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:8px 14px;' +
      'text-align:center;z-index:99;border-top:1px solid #fcd34d';
  } else {
    b.textContent = '实时数据 · 刷新页面即同步最新用量';
    b.style.cssText = 'position:fixed;right:14px;bottom:12px;background:#ecfdf5;color:#065f46;' +
      'font:11.5px/1.4 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;padding:5px 12px;' +
      'border-radius:13px;border:1px solid #a7f3d0;z-index:99';
  }
  document.body.appendChild(b);
})();
</script>
"""

CSS = """
:root { --ink:#1f2937; --sub:#6b7280; --line:#e5e7eb; --bg:#f7f8fa; --card:#ffffff; --accent:#2563eb; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 20px 48px; background:var(--bg); color:var(--ink);
       font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
       font-variant-numeric:tabular-nums; }
.wrap { max-width:1120px; margin:0 auto; }
h1 { font-size:21px; margin:0 0 4px; }
.meta { color:var(--sub); font-size:12.5px; margin-bottom:20px; }
.meta b { color:var(--ink); font-weight:600; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; margin-bottom:22px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.card .k { font-size:12px; color:var(--sub); }
.card .v { font-size:22px; font-weight:650; margin:2px 0; }
.card .v.mdl { font-size:17px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.card .s { font-size:12px; color:var(--sub); }
section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin-bottom:18px; }
h2 { font-size:15.5px; margin:0 0 12px; }
h2 .note { font-weight:400; font-size:12px; color:var(--sub); margin-left:8px; }
.chart { width:100%; height:auto; }
.grid { stroke:#eef0f3; stroke-width:1; }
.axis { font-size:10.5px; fill:#9ca3af; }
.val  { font-size:10px; fill:#4b5563; }
.rowlabel { font-size:11.5px; fill:#374151; }
.rowtotal { font-size:11px; fill:#6b7280; }
.heatmap { min-width:720px; }
.scroll { overflow:auto; max-height:400px; }
.scroll thead th { position:sticky; top:0; background:#fff; z-index:2; }
.rank thead th:nth-child(1), .rank thead th:nth-child(2) { z-index:3; }
/* ---- 趋势图：固定 y 轴 + 可横滑绘图区 ---- */
.chartview { position:relative; }
.chartrow { display:flex; align-items:flex-start; }
.yaxis { flex:0 0 62px; width:62px; height:300px; }
.scrollx { flex:1 1 auto; min-width:0; overflow-x:auto; overflow-y:hidden; }
.scrollx svg { display:block; }
.chartwide { display:block; max-width:none; height:300px; }
/* ---- 热力图（HTML 表格版）：模型名固定，色块与日期一起横滑 ---- */
.hmwrap { overflow-x:auto; width:100%; }
.hm { border-collapse:separate; border-spacing:3px 2px; table-layout:fixed; }
.hm th { font-weight:400; }
.hm thead th { font-size:10.5px; color:var(--sub); text-align:center; width:54px; height:16px; white-space:nowrap; }
.hm .hmlabel { width:152px; text-align:left; padding-left:8px; padding-right:0; font-size:11.5px; color:var(--ink);
               position:sticky; left:0; background:#fff; z-index:1; white-space:nowrap;
               overflow:hidden; text-overflow:ellipsis; }
.hm thead .hmlabel, .hm thead .hmtotal { text-align:center; padding-left:0; padding-right:0; z-index:2; }
.hm .hmtotal { width:92px; text-align:left; padding-left:10px; font-size:11px; color:var(--sub);
               white-space:nowrap; position:sticky; right:0; background:#fff; z-index:1;
               box-shadow:-2px 0 0 var(--line); }
.hm thead .hmtotal { z-index:2; }
.hm td.cell { width:54px; height:24px; padding:0; }
.hm td.cell i { display:block; width:54px; height:24px; border-radius:3px; }
table { width:100%; border-collapse:collapse; font-size:12.8px; }
th { text-align:center; font-weight:600; color:var(--sub); border-bottom:1.5px solid var(--line); padding:7px 8px; white-space:nowrap; }
td { text-align:left; border-bottom:1px solid #f2f3f5; padding:7px 8px; white-space:nowrap; }
tr:hover td { background:#f9fafb; }
.barbg { display:inline-block; width:54px; height:8px; background:#e8edf5; border-radius:2px;
         vertical-align:middle; margin-right:6px; overflow:hidden; }
.bar { display:block; height:100%; background:#3b82f6; border-radius:2px; }
/* ---- 宽表：固定列宽 + 左列冻结 + 容器内横向滚动（防止页面级滚动错位） ---- */
table.rank, table.daily { table-layout:fixed; border-collapse:separate; border-spacing:0; }
table.rank   { width:1044px; min-width:100%; }
table.daily  { width:668px; min-width:100%; }
.rank th, .rank td { overflow:hidden; }
.rank td.mname { text-overflow:ellipsis; white-space:nowrap; }
.rank td.mcli  { white-space:nowrap; }
.rank th:nth-child(1), .rank td:nth-child(1) { position:sticky; left:0;    width:34px; background:#fff; z-index:1; }
.rank th:nth-child(2), .rank td:nth-child(2) { position:sticky; left:34px; background:#fff; z-index:1; }
.rank thead th:nth-child(1), .rank thead th:nth-child(2) { z-index:2; }
.rank td:nth-child(2) { box-shadow:2px 0 0 var(--line); }
.rank tbody tr:hover td { background:#f9fafb; }
/* ---- 趋势图悬停提示 ---- */
.chartwrap { position:relative; }
.hov { cursor:crosshair; }
.tip { position:absolute; top:4px; left:0; opacity:0; transition:opacity .12s; pointer-events:none; z-index:5;
       background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:0 4px 18px rgba(17,24,39,.12);
       padding:9px 13px; font-size:12px; line-height:1.75; white-space:nowrap; }
.tip .tip-d { color:var(--sub); font-size:11.5px; margin-bottom:1px; }
.tip .tip-r i { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; }
.tip .tip-r b { font-weight:650; }
/* ---- 今日/本周/本月表 ---- */
table.periods { table-layout:fixed; width:754px; min-width:100%; }
.periods td.tmodel { white-space:nowrap; }
.badge { display:inline-block; font-size:11px; padding:1px 7px; border-radius:9px; background:#eef2ff; color:#3730a3; margin-right:6px; }
.badge.codex { background:#ecfdf5; color:#065f46; }
.bill { font-size:11.5px; color:var(--sub); }
.compose { display:flex; height:22px; border-radius:6px; overflow:hidden; margin:6px 0 10px; }
.compose div { height:100%; }
.legend { font-size:12px; color:var(--sub); }
.legend span { display:inline-block; margin-right:16px; }
.dot { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; vertical-align:-1px; }
.foot { color:var(--sub); font-size:12px; line-height:1.8; }
.empty { color:var(--sub); }
"""


def build_html(agg, billing, args, db_path):
    by_model, by_day, by_md, dates = agg
    totals = dict(events=0, tin=0, tout=0, cread=0, cwrite=0, reasoning=0)
    for m in by_model.values():
        for k in totals:
            totals[k] += m[k]
    grand = sum(totals.values())
    daily_rows = [dict(date=d, **by_day[d]) for d in dates]

    clients = defaultdict(int)
    for (client, _model), m in by_model.items():
        clients[client] += m["tin"] + m["tout"] + m["cread"] + m["cwrite"] + m["reasoning"]

    # ---- KPI 卡片
    peak_day = max(daily_rows, key=lambda r: r["tin"] + r["tout"] + r["cread"] + r["cwrite"] + r["reasoning"]) if daily_rows else None
    # 单日模型峰值：所有（模型 × 日期）组合中单日消耗最大者
    if by_md:
        (md_day, md_agent), md_val = max(by_md.items(), key=lambda kv: kv[1])
        _c, md_model = split_agent(md_agent)
        md_v, md_s = md_model, f"{md_day} · {fmt_compact(md_val)}"
    else:
        md_v, md_s = "-", "-"
    prompt_tokens = totals["tin"] + totals["cread"]
    cache_ratio = totals["cread"] / prompt_tokens if prompt_tokens else 0
    cards = [
        ("Token 总用量", fmt_compact(grand), f"精确 {fmt_int(grand)}"),
        ("调用次数", fmt_int(totals["events"]), f"日均 {fmt_int(totals['events'] / max(len(dates), 1))}"),
        ("活跃天数", str(len(dates)), f"{dates[0]} ~ {dates[-1]}" if dates else "-"),
        ("缓存读取占比", fmt_pct(cache_ratio), f"{fmt_compact(totals['cread'])} token"),
        ("调用模型数量", str(len(by_model)), " / ".join(f"{CLIENT_NAMES.get(c, c)} {sum(1 for (cc, _m) in by_model if cc == c)}" for c in clients)),
        ("单日 Token 峰值", fmt_compact(peak_day["tin"] + peak_day["tout"] + peak_day["cread"] + peak_day["cwrite"] + peak_day["reasoning"]) if peak_day else "-", peak_day["date"] if peak_day else "-"),
        ("单日模型 Token 峰值", md_v, md_s),
    ]

    def _card(k, v, s):
        # 模型名可能较长：降字号 + 省略号 + title 全称
        long = len(str(v)) > 12
        cls = "v mdl" if long else "v"
        title = f" title='{v}'" if long else ""
        return f"<div class='card'><div class='k'>{k}</div><div class='{cls}'{title}>{v}</div><div class='s'>{s}</div></div>"

    cards_html = "".join(_card(k, v, s) for k, v, s in cards)

    # ---- 今日 / 本周 / 本月
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())  # 周一起算
    month_start = today.replace(day=1)
    today_s = today.isoformat()

    def _period(start_s, end_s):
        ev = sum(by_day[d]["events"] for d in dates if start_s <= d <= end_s)
        tt = sum(by_day[d]["tin"] + by_day[d]["tout"] + by_day[d]["cread"] + by_day[d]["cwrite"] + by_day[d]["reasoning"]
                 for d in dates if start_s <= d <= end_s)
        per_model = defaultdict(int)
        for (d, agent), v in by_md.items():
            if start_s <= d <= end_s:
                per_model[agent] += v
        top = max(per_model.items(), key=lambda kv: kv[1]) if per_model else None
        return ev, tt, top

    period_rows_html = []
    for label, s0 in (("今日", today_s), ("本周", week_start.isoformat()), ("本月", month_start.isoformat())):
        ev, tt, top = _period(s0, today_s)
        rng = today_s if label == "今日" else f"{s0[5:]} ~ {today_s[5:]}"
        if tt > 0 and top:
            _c, tm = split_agent(top[0])
            model_cell, usage_cell = tm, fmt_compact(top[1])
        else:
            model_cell, usage_cell = "—", "—"
        period_rows_html.append(
            f"<tr><td><b>{label}</b></td><td>{rng}</td>"
            f"<td><b>{fmt_compact(tt) if tt else '0'}</b></td>"
            f"<td>{fmt_int(ev) if ev else '0'}</td>"
            f"<td class='tmodel'>{model_cell}</td><td>{usage_cell}</td></tr>"
        )
    periods_html = "".join(period_rows_html)

    # ---- 构成条
    comp = [("输入", totals["tin"], "#3b82f6"), ("输出", totals["tout"], "#f59e0b"),
            ("缓存读取", totals["cread"], "#10b981"), ("推理", totals["reasoning"], "#8b5cf6"),
            ("缓存写入", totals["cwrite"], "#64748b")]
    comp = [c for c in comp if c[1] > 0]
    segs = "".join(f"<div style='width:{v / grand * 100:.3f}%;background:{color}' title='{name} {fmt_int(v)}'></div>"
                   for name, v, color in comp)
    legend = "".join(f"<span><i class='dot' style='background:{color}'></i>{name} {fmt_compact(v)}（{fmt_pct(v / grand)}）</span>"
                     for name, v, color in comp)

    # ---- 模型排行
    ranking = sorted(
        by_model.items(),
        key=lambda kv: kv[1]["tin"] + kv[1]["tout"] + kv[1]["cread"] + kv[1]["cwrite"] + kv[1]["reasoning"],
        reverse=True,
    )
    top_total = sum(ranking[0][1][k] for k in ("tin", "tout", "cread", "cwrite", "reasoning")) if ranking else 1
    rows_html = []
    for i, ((client, model), m) in enumerate(ranking, 1):
        total = m["tin"] + m["tout"] + m["cread"] + m["cwrite"] + m["reasoning"]
        prompt = m["tin"] + m["cread"]
        hit = m["cread"] / prompt if prompt else 0
        kind = billing.get(f"{client}-model:{model}", "—")
        badge = CLIENT_NAMES.get(client, client)
        rows_html.append(
            f"<tr><td>{i}</td><td class='mname' title='{model}'><b>{model}</b></td>"
            f"<td class='mcli'><span class='badge {client}'>{badge}</span><span class='bill'>{kind}</span></td>"
            f"<td>{fmt_int(m['events'])}</td><td>{fmt_compact(m['tin'])}</td><td>{fmt_compact(m['tout'])}</td>"
            f"<td>{fmt_compact(m['cread'])}</td><td>{fmt_pct(hit)}</td>"
            f"<td><b>{fmt_compact(total)}</b></td>"
            f"<td><span class='barbg'><span class='bar' style='width:{total / top_total * 100:.1f}%'></span></span>{fmt_pct(total / grand)}</td></tr>"
        )

    # ---- 每日明细
    day_rows = "".join(
        f"<tr><td>{r['date']}</td><td>{fmt_int(r['events'])}</td><td>{fmt_compact(r['tin'])}</td>"
        f"<td>{fmt_compact(r['tout'])}</td><td>{fmt_compact(r['cread'])}</td>"
        f"<td><b>{fmt_compact(r['tin'] + r['tout'] + r['cread'] + r['cwrite'] + r['reasoning'])}</b></td>"
        f"<td>{fmt_pct((r['tin'] + r['tout'] + r['cread'] + r['cwrite'] + r['reasoning']) / grand)}</td></tr>"
        for r in reversed(daily_rows)
    )

    top_agents = [f"{client}-model:{model}" for (client, model), _m in ranking[: args.top]]
    heatmap = heatmap_html(by_md, top_agents, dates, args.top)
    axis_svg, area, trend_js = svg_daily_area(daily_rows)
    trend_script = TREND_SCRIPT.replace("__DATA__", json.dumps(trend_js, ensure_ascii=False))
    banner_script = BANNER_SCRIPT.replace("__LIVE_URL__", LIVE_URL)
    gen_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    source_desc = {
        "auto": "WorkBuddy 原始会话直读 + HUD 账本（其他客户端）",
        "transcripts": "WorkBuddy 原始会话直读",
        "ledger": "Agent HUD 本地账本",
    }.get(getattr(args, "source", "auto"), "WorkBuddy 原始会话直读 + HUD 账本（其他客户端）")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 模型 Token 用量统计</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>AI 模型 Token 用量统计</h1>
  <div class="meta">数据范围 <b>{dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'}</b>（{len(dates)} 天） ·
    数据源 <b>{source_desc}</b> · 生成于 {gen_at} · token-usage-stats v{VERSION}</div>

  <div class="cards">{cards_html}</div>

  <section>
    <h2>今日 / 本周 / 本月<span class="note">按本地时间、周一起算；最高模型 = 该时段 token 消耗第一的模型</span></h2>
    <div class="scroll">
    <table class="periods">
      <colgroup><col style="width:90px"><col style="width:140px"><col style="width:112px"><col style="width:100px"><col style="width:200px"><col style="width:112px"></colgroup>
      <thead><tr><th>时段</th><th>日期范围</th><th>Token 总用量</th><th>调用次数</th><th>消耗最高模型</th><th>Token 用量</th></tr></thead>
      <tbody>{periods_html}</tbody>
    </table>
    </div>
  </section>

  <section>
    <h2>每日消耗趋势<span class="note">按日堆叠：输入 / 输出 / 缓存读取；横向滑动查看更早日期，悬停任意日期查看当日明细</span></h2>
    <div class="legend">
      <span><i class="dot" style="background:#3b82f6"></i>输入</span>
      <span><i class="dot" style="background:#f59e0b"></i>输出</span>
      <span><i class="dot" style="background:#10b981"></i>缓存读取</span>
      <span><i class="dot" style="background:#111827"></i>当日总量（悬停查看）</span>
    </div>
    <div class="chartview">
      <div class="chartrow">
        {axis_svg}
        <div class="scrollx">{area}</div>
      </div>
      <div class="tip" id="tip"></div>
    </div>
  </section>

  <section>
    <h2>Token 构成<span class="note">全期累计</span></h2>
    <div class="compose">{segs}</div>
    <div class="legend">{legend}</div>
  </section>

  <section>
    <h2>各模型 × 每日消耗热力图<span class="note">Top {min(args.top, len(ranking))} 模型，对数色阶；模型名固定，色块与日期可横向滑动；悬停查看精确值</span></h2>
    {heatmap}
  </section>

  <section>
    <h2>模型用量排行<span class="note">按总 token 降序；缓存命中率 = 缓存读取 /（输入 + 缓存读取）</span></h2>
    <div class="scroll">
    <table class="rank">
      <colgroup><col style="width:34px"><col style="width:176px"><col style="width:208px"><col style="width:76px"><col style="width:76px"><col style="width:70px"><col style="width:88px"><col style="width:88px"><col style="width:112px"><col style="width:116px"></colgroup>
      <thead><tr><th>#</th><th>模型</th><th>客户端 / 计费方式</th><th>调用次数</th><th>输入</th><th>输出</th>
      <th>缓存读取</th><th>缓存命中率</th><th>Token 总用量</th><th>占比</th></tr></thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
  </section>

  <section>
    <h2>每日明细</h2>
    <div class="scroll">
    <table class="daily">
      <colgroup><col style="width:100px"><col style="width:92px"><col style="width:88px"><col style="width:88px"><col style="width:100px"><col style="width:116px"><col style="width:88px"></colgroup>
      <thead><tr><th>日期</th><th>调用次数</th><th>输入</th><th>输出</th><th>缓存读取</th><th>Token 总用量</th><th>占全期</th></tr></thead>
      <tbody>{day_rows}</tbody>
    </table>
    </div>
  </section>

  <div class="foot">
    数据说明：① WorkBuddy 用量直接读取原始会话文件（按会话内容定位，不受工作目录改名影响），其他客户端读 Agent HUD 账本，均为只读；
    ② 统计口径已用 HUD 完好窗口（2026-10-15~10-20）校准，逐日偏差 +0.1%~+0.3%；
    ③ 缓存读取 token 计入总量（输入/输出/缓存拆分见上表）；④ 历史覆盖全部原始会话（自 2026-05-28 起），比 HUD 账本记录（2026-10-07 起）更完整；
    ⑤ 模型标识沿用原始 ID；⑥ 原始数据见同目录 CSV（by-model / daily / model-daily 三份，UTF-8 BOM，可直接用 Excel 打开）。
  </div>
</div>
{trend_script}
{banner_script}
</body>
</html>"""


# ---------------------------------------------------------------- CSV

def write_csvs(out_dir: Path, agg):
    by_model, by_day, by_md, dates = agg
    p1 = out_dir / "token-usage-by-model.csv"
    ranking = sorted(by_model.items(),
                     key=lambda kv: kv[1]["tin"] + kv[1]["tout"] + kv[1]["cread"] + kv[1]["cwrite"] + kv[1]["reasoning"],
                     reverse=True)
    grand = sum(m["tin"] + m["tout"] + m["cread"] + m["cwrite"] + m["reasoning"] for m in by_model.values())
    with open(p1, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["client", "model", "events", "tokens_in", "tokens_out", "cache_read",
                    "cache_write", "reasoning", "total_tokens", "share_pct", "active_days", "avg_tokens_per_event"])
        for (client, model), m in ranking:
            total = m["tin"] + m["tout"] + m["cread"] + m["cwrite"] + m["reasoning"]
            w.writerow([client, model, m["events"], m["tin"], m["tout"], m["cread"], m["cwrite"], m["reasoning"],
                        total, f"{total / grand * 100:.2f}", len(m["days"]), round(total / m["events"], 1)])
    p2 = out_dir / "token-usage-daily.csv"
    with open(p2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "events", "tokens_in", "tokens_out", "cache_read", "cache_write", "reasoning", "total_tokens"])
        for d in dates:
            r = by_day[d]
            w.writerow([d, r["events"], r["tin"], r["tout"], r["cread"], r["cwrite"], r["reasoning"],
                        r["tin"] + r["tout"] + r["cread"] + r["cwrite"] + r["reasoning"]])
    p3 = out_dir / "token-usage-model-daily.csv"
    with open(p3, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "client", "model", "total_tokens"])
        for d in dates:
            for (client, model), _m in ranking:
                v = by_md.get((d, f"{client}-model:{model}"), 0)
                if v:
                    w.writerow([d, client, model, v])
    return p1, p2, p3


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="AI 模型 Token 用量统计（v2：transcripts 直读 + HUD 账本混合）")
    ap.add_argument("--days", type=int, default=0, help="仅统计最近 N 天（0 = 全部历史）")
    ap.add_argument("--top", type=int, default=12, help="热力图显示的模型数（默认 12）")
    ap.add_argument("--source", choices=("auto", "transcripts", "ledger"), default="auto",
                    help="auto=WorkBuddy 直读 transcripts + 其他客户端读账本（默认）；transcripts=仅原会话；ledger=仅 HUD 账本（旧行为）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="HUD 账本路径")
    ap.add_argument("--billing-report", type=Path, default=DEFAULT_BILLING_REPORT, help="HUD 最近用量报告（计费方式归类，可选）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--quiet", action="store_true", help="减少输出")
    args = ap.parse_args()

    rows = gather_rows(args.db, args.days or None, args.source)
    if not rows:
        print("[!] 所选时间范围内没有调用记录。")
        return 1
    agg = aggregate(rows)
    billing = load_billing_kinds(args.billing_report)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    html = build_html(agg, billing, args, args.db)
    html_path = args.out_dir / "token-usage-report.html"
    html_path.write_text(html, encoding="utf-8")
    csvs = write_csvs(args.out_dir, agg)

    if not args.quiet:
        by_model, by_day, by_md, dates = agg
        grand = sum(m["tin"] + m["tout"] + m["cread"] + m["cwrite"] + m["reasoning"] for m in by_model.values())
        top3 = sorted(by_model.items(),
                      key=lambda kv: kv[1]["tin"] + kv[1]["tout"] + kv[1]["cread"] + kv[1]["cwrite"] + kv[1]["reasoning"],
                      reverse=True)[:3]
        print(f"统计完成：{len(rows)} 次调用 / {len(dates)} 天 / {len(by_model)} 个模型 / 共 {fmt_compact(grand)} tokens")
        print("Top 3：" + "；".join(f"{m} {fmt_compact(v['tin'] + v['tout'] + v['cread'] + v['cwrite'] + v['reasoning'])}" for (_c, m), v in top3))
        print(f"HTML 报告: {html_path}")
        for p in csvs:
            print(f"CSV:       {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
