#!/usr/bin/env python3
"""
token-usage-server.py — Token 用量统计「本地服务」

背景：浏览器（Safari）在 file:// 下无法读取本地目录（无 File System Access API），
      静态页面每次都要手动选文件夹。本服务常驻本机，直接读会话文件，
      页面加载即自动取数、按刷新即最新——不需要任何上传。

架构：
  Safari → http://127.0.0.1:8791/         → 应用页面（web/index.html）
         → /api/events?sub=0|1            → 逐事件数据（紧凑数组，浏览器端聚合）
         → /report                        → 旧版单页 HTML 报告（保留）
  所有响应 Cache-Control: no-store，保证刷新必取新数据。

路由：
  GET /                       应用页面
  GET /app.js                 应用逻辑
  GET /token-semantics.js     语义层（前端聚合用）
  GET /api/events?sub=0|1     数据接口（sub=1 计入子代理会话）
  GET /report                 旧版统计报告
  GET /api/stats              JSON 摘要（调试用）
  GET /healthz                健康检查
  GET /favicon.ico            204

用法：
  python3 token-usage-server.py [--port 8791] [--host 127.0.0.1]
  生产形态：LaunchAgent（cn.workbuddy.token-usage-server），RunAtLoad + KeepAlive。
  改完本文件后的重启命令：
    launchctl kickstart -k gui/$(id -u)/cn.workbuddy.token-usage-server
"""
import argparse
import importlib.util
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent

# 模块文件名带连字符（token-usage-stats.py），无法用 import 语句，需按路径加载
_spec = importlib.util.spec_from_file_location("token_usage_stats", _HERE / "token-usage-stats.py")
tus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tus)

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import wb_token_semantics as sem  # noqa: E402
import wb_quota                  # noqa: E402  额度窗口（rate limit）
import wb_credits                # noqa: E402  WorkBuddy 积分（本地）
import wb_online                 # noqa: E402  可选的联网读取（默认关闭）

WEB_DIR = _HERE / "web"
PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"
DB_PATH = tus.DEFAULT_DB

# ---------------------------------------------------------------- 四源根目录
# 默认路径取各客户端自己的约定，并尊重它们的环境变量（便于非标准安装 / Windows）：
#   Codex CLI   $CODEX_HOME/sessions        （默认 ~/.codex/sessions）
#   Claude Code $CLAUDE_CONFIG_DIR/projects （默认 ~/.claude/projects）
#   DeepSeek Harness $DSH_HOME/sessions     （默认 ~/.dsh/sessions）
_SRC_DEFAULT = {
    "workbuddy": (Path.home() / ".workbuddy" / "projects", None, None),
    "codex": (Path.home() / ".codex" / "sessions", "CODEX_HOME", "sessions"),
    "claude": (Path.home() / ".claude" / "projects", "CLAUDE_CONFIG_DIR", "projects"),
    "dsh": (Path.home() / ".dsh" / "sessions", "DSH_HOME", "sessions"),
}
ENABLED_SOURCES = ("workbuddy", "codex", "claude", "dsh")
USE_LEDGER = False          # 账本读的就是上面这些文件的二手结果，同时用会重复计数


def active_roots() -> dict:
    """按 --sources 过滤、且目录确实存在的数据源根目录。

    不存在的目录直接不传：collect_all 会跳过，界面则显示"该源未找到"，
    而不是把它当成"0 用量"。
    """
    out: dict[str, Path] = {}
    for name in ENABLED_SOURCES:
        spec = _SRC_DEFAULT.get(name)
        if not spec:
            continue
        default, env, sub = spec
        env_val = os.environ.get(env) if env else None
        p = (Path(env_val) / sub) if (env_val and sub) else (Path(env_val) if env_val else default)
        if p.is_dir():
            out[name] = p
    if USE_LEDGER and DB_PATH.exists():
        out["ledger"] = DB_PATH
    return out

TOP_N = 12
_LAST = {"events": 0, "ms": 0.0}
_QUOTA_TTL = 20.0         # 额度缓存秒数：额度是"当前值"，不能永久缓存

# 静态资源白名单：只允许这几个文件，不做通用目录映射（防路径穿越）
# 主页面：同源脚本 + 内联样式（本项目大量用 style 属性）；禁止外部资源与框架嵌入
PAGE_CSP = {
    "Content-Security-Policy":
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
}
# 旧版报告页：连脚本都不允许
REPORT_CSP = {
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'"
}

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/token-semantics.js": ("token-semantics.js", "application/javascript; charset=utf-8"),
    "/cost.js": ("cost.js", "application/javascript; charset=utf-8"),
    "/prices.js": ("prices.js", "application/javascript; charset=utf-8"),
}
# 注意：golden-dev.js 故意不在白名单里 —— 那是开发机上的自检基准，不对页面提供。


# ---------------------------------------------------------------- 数据接口


def _files_by_project(include_sub: bool) -> dict:
    """各项目目录下的文件数（覆盖诊断用）。

    workbuddy 按 <项目>/<会话>.jsonl 两层；其余三源的文件不在项目目录下，
    因此用「来源前缀 + 最近一级有意义目录」作键，与事件侧的 project 命名保持一致。
    """
    counts: dict[str, int] = {}
    wb = PROJECTS_DIR
    if wb.is_dir():
        for f in wb.glob("*/*.jsonl"):
            counts[f.parent.name] = counts.get(f.parent.name, 0) + 1
        if include_sub:
            for f in wb.glob("*/*/subagents/*.jsonl"):
                proj = f.parent.parent.parent.name
                counts[proj] = counts.get(proj, 0) + 1
    for name, root in active_roots().items():
        if name == "workbuddy" or name == "ledger":
            continue
        if name == "codex":
            n = sum(1 for _ in root.glob("**/rollout-*.jsonl"))
        elif name == "claude":
            n = sum(1 for p in root.glob("**/*.jsonl") if "subagents" in p.parts) if include_sub \
                else sum(1 for p in root.glob("**/*.jsonl") if "subagents" not in p.parts)
        else:
            n = sum(1 for _ in root.glob("**/session*.jsonl*"))
        if n:
            counts[f"{name}/（全部会话）"] = n
    return counts


def build_events_json(include_sub: bool = False) -> dict:
    """把逐事件数据压成紧凑数组返回，由浏览器端展开并聚合。

    字段顺序（与 web/app.js 的 decodePayload 必须一致）：
      [ts, agentIdx, sessionIdx, projectIdx, tin, cread, tout, cwrite, reasoning]
    sessionIdx = -1 表示无会话标识。
    """
    t0 = time.time()
    session_meta: dict = {}
    stats: dict = {}
    quota_sink: list = []          # 顺手带出 Codex 的 rate_limits，避免二次扫描
    events = list(sem.collect_all(
        active_roots(), include_subagents=include_sub,
        session_meta=session_meta, stats=stats, use_ledger=USE_LEDGER,
        quota_out=quota_sink))

    agents: list[str] = []
    agent_ix: dict[str, int] = {}
    sessions: list[str] = []
    session_ix: dict[str, int] = {}
    projects: list[str] = []
    project_ix: dict[str, int] = {}
    rows: list[list] = []

    for ev in events:
        a = ev["agent"]
        ai = agent_ix.get(a)
        if ai is None:
            ai = len(agents)
            agent_ix[a] = ai
            agents.append(a)

        s = ev.get("session")
        if s:
            si = session_ix.get(s)
            if si is None:
                si = len(sessions)
                session_ix[s] = si
                sessions.append(s)
        else:
            si = -1

        p = ev.get("project") or "(未知)"
        pi = project_ix.get(p)
        if pi is None:
            pi = len(projects)
            project_ix[p] = pi
            projects.append(p)

        rows.append([ev["ts_ms"], ai, si, pi,
                     ev["tin"], ev["cread"], ev["tout"], ev["cwrite"], ev["reasoning"]])

    _LAST["events"] = len(rows)
    _LAST["ms"] = round((time.time() - t0) * 1000, 1)

    # 额度窗口：Codex 用刚顺手采到的 rate_limits，其余来源读 HUD 账本
    quota_groups = wb_quota.group_and_sort(
        wb_quota.collect_quota(active_roots(), codex_sink=quota_sink, ledger_db=DB_PATH))
    _LAST["quota"] = quota_groups
    credits = wb_credits.workbuddy_credits()
    _LAST["credits"] = credits

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "include_subagents": include_sub,
        "events": len(rows),
        "agents": agents,
        "sessions": sessions,
        # 与 sessions 一一对应的会话名称；ai-title 优先，首条用户消息兜底，空串表示取不到
        "titles": [
            (session_meta.get(_s, {}).get("title")
             or session_meta.get(_s, {}).get("preview")
             or "") for _s in sessions
        ],
        "projects": projects,
        "files_by_project": _files_by_project(include_sub),
        # 各数据源本次读到的文件数/事件数/异常说明，供界面显示"读到什么、缺什么"
        "sources": stats,
        # 额度窗口（按套餐分组、按最近观测倒序）与积分概要，界面概览区直接用
        "quota": quota_groups,
        "credits": _credits_brief(credits),
        "rows": rows,
    }


def _credits_brief(c: dict) -> dict:
    """积分概要。

    只下发汇总：积分来自 workbuddy.db 的 credit_json（逐次调用的真实消耗），
    不是 session_usage.used（那是上下文 token 占用，2026-10-23 已校正）。
    本月/近 30 天按会话的 updated_at 归期，属近似值。
    """
    return {
        "available": c.get("available"),
        "note": c.get("note"),
        "total_credit": c.get("total_credit", 0.0),
        "credit_this_month": c.get("credit_this_month", 0.0),
        "credit_last30": c.get("credit_last30", 0.0),
        "first_month": c.get("first_month"),
        "sessions_billed": c.get("sessions_billed", 0),
        "sessions_total": c.get("sessions_total", 0),
        "calls": c.get("calls", 0),
    }


def _set_plan_quota(enabled: bool) -> tuple[bool, str]:
    """把 plan_quota 写进 online.json（只动这一个布尔，其余字段原样保留）。

    界面上的开关走这里——用户不该为了开一个开关去手编 JSON。Key 不读不写，
    仍然由服务进程在请求时从 models.json 本地取得。
    """
    p = wb_online.CONFIG_PATH
    cfg: dict = {}
    try:
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
    except Exception as e:
        return False, f"配置文件解析失败：{type(e).__name__}"
    cfg["plan_quota"] = bool(enabled)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 文件里可能有用户填的 API Key，权限固定 600
        try:
            p.chmod(0o600)
        except OSError:
            pass
    except Exception as e:
        return False, f"写入失败：{type(e).__name__}"
    _LAST.pop("quota", None)      # 下一次 /api/quota 立即重算，不等 TTL
    return True, "已开启" if enabled else "已关闭"


def build_quota_json() -> dict:
    """额度窗口 + 积分 + 可选联网数据 + 外部套餐（API 接入的 Coding Plan）。"""
    groups = _LAST.get("quota")
    # 缓存必须带 TTL：额度是"当前还剩多少"，缓存不过期会让界面永远停在
    # 第一次取数时的旧值（本机实测：15:45 缓存了 GLM 的旧样本，之后手动
    # 刷新、自动刷新都不更新，用户看到的一直是"上周的数据"）。
    # 20 秒 TTL 对 60 秒的自动刷新足够（每次都拿到新值），又不至于每次
    # 请求都把 400MB transcripts 重读一遍。
    if groups is None or (time.time() - _LAST.get("quota_at", 0)) > _QUOTA_TTL:
        quota_sink: list = []
        list(sem.collect_all(active_roots(), quota_out=quota_sink))
        groups = wb_quota.group_and_sort(
            wb_quota.collect_quota(active_roots(), codex_sink=quota_sink,
                                   ledger_db=DB_PATH))
        _LAST["quota"] = groups
        _LAST["quota_at"] = time.time()
    # 积分同样要每次重读：它来自 workbuddy.db（几毫秒），缓存会让新发生的
    # 调用显示不出来——正是本轮「刷新不更新」的同类问题。
    credits = wb_credits.workbuddy_credits()
    _LAST["credits"] = credits
    online = wb_online.fetch_online()       # 未启用时完全不联网
    plans = _build_plans(online, groups)
    # 官方接口拿到了窗口的套餐，把 HUD 那份同厂商的分组从列表里去掉——
    # 否则界面上同一个套餐出现两张卡（HUD 旧值 + 官方新值），用户无法判断该信哪个。
    if plans.get("rows"):
        covered_clients = {c.casefold() for r in plans["rows"] if r.get("windows")
                           for c in r.get("hud_clients", [])}
        if covered_clients:
            groups = [g for g in groups
                      if (g.get("client") or "").casefold() not in covered_clients]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "quota": groups,
        "credits": credits,
        "credits_brief": _credits_brief(credits),
        "online": online,
        "plans": plans["rows"],
        "plans_missing": plans.get("missing", []),
        "plan_quota_enabled": plans["enabled"],
        "plan_quota_how": plans["how"],
        "unmatched_gateways": plans["unmatched"],
        # 适配登记表：支持哪些客户端、各自能力与本机是否探测到
        "registry": sem.sources_info(),
        "supported": {
            # 界面据此说明"哪些来源有额度/积分数据"，避免用户误以为没装
            "quota": ["codex", "cursor", "copilot", "open-agents"],
            "credits_local": ["workbuddy"],
            "credits_online": ["workbuddy", "dsh"],
            "unsupported": ["claude", "dsh-local"],
        },
    }


def _build_plans(online: dict, groups=None) -> dict:
    """识别「通过 API 接入的外部套餐」并按需联网读取额度。

    设计约束（都来自隐私考量）：
      · Key 只在本进程内从 models.json 读取，**绝不进入下面的返回值**；
      · 默认不联网：online.json 里 plan_quota 不为 true 时只做识别、不发请求；
      · 未核实响应结构的 probe（status=probe）一律不请求。

    groups
      已归组的本地额度窗口。若某个 probe 的套餐已经由 HUD 账本提供
      （probe.hud_clients 命中），就不再显示"未开启"的劝告——那会造成
      同一个套餐在界面上出现两次（一次有数据、一次空劝告）。
    """
    plans, keys = wb_quota.read_plan_models()
    cfg = wb_online.load_config()
    enabled = bool(cfg.get("plan_quota"))
    # 界面已有开关，这里只说"点哪里"，不再教用户改配置文件
    how = "点击上方「厂商额度查询」开关即可开启"

    # 已经被 HUD 账本覆盖的客户端（按 client 名，大小写不敏感）
    hud_clients = {(g.get("client") or "").casefold() for g in (groups or [])}

    # models.json 里接了外部 API 但没登记额度端点的网关：明确列出来，
    # 而不是静默忽略——用户需要知道"这些还没覆盖"
    unmatched: list[dict] = []
    matched_hosts = {p["gateway"] for p in plans}
    try:
        arr = json.loads((Path.home() / ".workbuddy" / "models.json")
                         .read_text(encoding="utf-8", errors="replace"))
    except Exception:
        arr = []
    if isinstance(arr, list):
        seen_hosts: set[str] = set()
        for m in arr:
            if not isinstance(m, dict):
                continue
            base = str(m.get("baseUrl") or m.get("base_url") or m.get("url") or "")
            if not base or "://" not in base:
                continue
            host = base.split("/")[2]
            if host in matched_hosts or host in seen_hosts:
                continue
            seen_hosts.add(host)
            unmatched.append({
                "gateway": host,
                "model_name": str(m.get("name") or m.get("id") or "")[:40],
            })

    rows: list[dict] = []
    for p in plans:
        row = dict(p)
        probe = next((x for x in wb_quota.PLAN_PROBES if x["id"] == p["probe_id"]), None)
        covered_by = [c for c in (probe or {}).get("hud_clients", [])
                      if c.casefold() in hud_clients]
        row["hud_covered"] = bool(covered_by)
        row["hud_clients"] = (probe or {}).get("hud_clients", [])
        row["enabled"] = enabled
        row["windows"] = []
        row["plan"] = None
        row["fetched_at"] = None
        row["error"] = None
        if enabled and probe:
            # 用户显式开启 plan_quota = 要最新数据。即使 HUD 已有该套餐的窗口
            # （hud_covered），也照样请求官方接口：HUD 的样本可能停更
            # （本机实测 GLM 的样本一度停在旧值），官方接口才是权威。
            # 请求失败则保留 HUD 那份（由 hud_covered 说明），不显示空窗口。
            r = wb_quota.fetch_plan_quota(probe, keys.get(p["probe_id"], ""))
            if r["rows"]:
                row["windows"] = r["rows"]
                row["plan"] = r["plan"]
                row["fetched_at"] = int(time.time())
                row["note"] = "来自厂商官方接口（plan_quota 开启后直连）"
            else:
                row["error"] = r["error"]
                if row["hud_covered"]:
                    row["note"] = ("官方接口暂不可用，上方额度卡显示的是 Agent HUD "
                                   "记录（可能不够新）")
        elif row["hud_covered"]:
            row["note"] = "额度已通过 Agent HUD 记录显示在上方额度卡中"
        rows.append(row)
    # 已登记但缺凭据的套餐：明确说出来，否则用户会以为这个渠道没被支持
    have = {r["probe_id"] for r in rows}
    missing = []
    for probe in wb_quota.PLAN_PROBES:
        if probe.get("local_key") and probe["id"] not in have:
            missing.append({
                "probe_id": probe["id"],
                "name": probe["name"],
                "hint": "未找到 API Key，余额暂不可读。请在本机 "
                        "~/.token-usage-stats/online.json 的 api_keys.deepseek "
                        "中填入（首次运行已自动生成该文件，保存后立即生效）。",
            })
    return {"rows": rows, "enabled": enabled, "how": how,
            "unmatched": unmatched, "missing": missing}


def build_stats_json() -> dict:
    """概要 JSON（调试用）。

    必须与 /api/events 同源：此前这里走的是旧的四源合并路径（WorkBuddy +
    HUD 账本），页面总量已经包含直读的 Codex/Claude/DSH，两边会对不上。
    """
    events = list(sem.collect_all(active_roots(), use_ledger=USE_LEDGER))
    if not events:
        return {"empty": True}
    agg = sem.aggregate(events)
    t = agg["totals"]
    days = sorted(agg["by_day"])
    return {
        "empty": False,
        "range": [days[0], days[-1]] if days else [],
        "days": len(days),
        "models": len(agg["by_model"]),
        "events": t["events"],
        "total_tokens": t["total"],
    }


def build_report_html(top: int = TOP_N) -> str:
    """旧版单页报告（/report）。"""
    rows = tus.gather_rows(tus.DEFAULT_DB, None, "auto")
    if not rows:
        return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>AI 模型 Token 用量统计</title></head><body style='padding:40px'>"
                "<h1>暂无调用记录</h1></body></html>")
    agg = tus.aggregate(rows)
    billing = tus.load_billing_kinds(tus.DEFAULT_BILLING_REPORT)
    ns = argparse.Namespace(top=top, source="auto")
    return tus.build_html(agg, billing, ns, tus.DEFAULT_DB)


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "TokenUsageStats/2.0"
    sys_version = ""                 # 不暴露 Python 版本（指纹信息）
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self._code = code
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        # 关键：禁止任何缓存，保证刷新必取新数据 / 新代码
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # 安全头：本机应用也要防被其他页面套壳 / 嗅探内容类型 / 外带 Referer
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        # 复用 _send，保证 HEAD 与 GET 的响应头（含安全头）完全一致；空 body 不写
        self._send(200, b"")

    def _host_ok(self) -> bool:
        """只接受本机 Host 头。

        写操作（切换开关）必须防 DNS rebinding：恶意页面可以把自己的域名解析到
        127.0.0.1，再用浏览器向本服务发 POST；此时浏览器带的 Host 是那个域名，
        不是 127.0.0.1，据此可以拦掉。
        """
        h = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return h in ("127.0.0.1", "localhost", "::1", "[::1]")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        t0 = time.time()
        self._code = 200
        try:
            if not self._host_ok():
                self._send(403, json.dumps({"ok": False, "error": "只接受本机访问"},
                                           ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            if path == "/api/plan-quota":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 64 * 1024:                 # 请求体上限，防本机页面灌大包
                    self._send(413, json.dumps({"ok": False, "error": "请求体过大"},
                                               ensure_ascii=False),
                               "application/json; charset=utf-8")
                    return
                raw = self.rfile.read(n) if n > 0 else b"{}"
                try:
                    req = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    req = {}
                enabled = bool(req.get("enabled")) if isinstance(req, dict) else False
                ok, msg = _set_plan_quota(enabled)
                self._send(200 if ok else 500,
                           json.dumps({"ok": ok, "enabled": enabled, "message": msg},
                                      ensure_ascii=False),
                           "application/json; charset=utf-8")
                return
            self._send(404, json.dumps({"ok": False, "error": "未知接口"},
                                       ensure_ascii=False),
                       "application/json; charset=utf-8")
        except Exception as e:
            self._send(500, json.dumps({"ok": False, "error": type(e).__name__},
                                       ensure_ascii=False),
                       "application/json; charset=utf-8")
        finally:
            self.log_message('"%s %s" -> %d (%.0fms)', self.command, self.path,
                             self._code, (time.time() - t0) * 1000)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        t0 = time.time()
        self._code = 200
        try:
            # GET 也必须校验 Host：DNS rebinding 下，恶意页面把自己的域名解析到
            # 127.0.0.1 后，浏览器发出的请求是同源的，能直接读到 /api/events 的
            # 响应（含项目路径、会话标题）。只挡 POST 等于把数据敞开。
            if not self._host_ok():
                self._send(403, "<h1>403</h1><p>只接受本机访问，"
                                "请通过 127.0.0.1 或 localhost 打开。</p>")
                return
            if path in STATIC:
                fname, ctype = STATIC[path]
                f = WEB_DIR / fname
                if not f.exists():
                    self._send(404, f"<h1>找不到 {fname}</h1>", "text/html; charset=utf-8")
                else:
                    self._send(200, f.read_bytes(), ctype, extra=PAGE_CSP)

            elif path == "/api/events":
                include_sub = qs.get("sub", ["0"])[0] in ("1", "true", "yes")
                self._send(200, json.dumps(build_events_json(include_sub), ensure_ascii=False),
                           "application/json; charset=utf-8")

            elif path == "/api/stats":
                self._send(200, json.dumps(build_stats_json(), ensure_ascii=False),
                           "application/json; charset=utf-8")

            elif path == "/api/quota":
                # 额度窗口 + 积分 + 可选联网（未配置时 online.enabled=false，不发任何请求）
                self._send(200, json.dumps(build_quota_json(), ensure_ascii=False),
                           "application/json; charset=utf-8")

            elif path == "/report":
                # 旧版页面未对模型名做 HTML 转义，用最严 CSP 兜底：
                # script-src 'none' 让任何注入的脚本与内联事件都无法执行。
                self._send(200, build_report_html(), extra=REPORT_CSP)

            elif path == "/healthz":
                self._send(200, "ok", "text/plain; charset=utf-8")

            elif path == "/favicon.ico":
                self._code = 204
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            else:
                self._send(404, "<h1>404</h1>")
        except Exception as e:
            self._send(500, f"<h1>服务端错误</h1><pre>{type(e).__name__}: {e}</pre>")
        finally:
            self.log_message('"%s %s" -> %d (%.0fms)', self.command, self.path,
                             self._code, (time.time() - t0) * 1000)

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))
        sys.stdout.flush()


def main():
    global TOP_N, ENABLED_SOURCES, USE_LEDGER
    ap = argparse.ArgumentParser(description="Token 用量统计本地服务")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="127.0.0.1", help="默认仅监听回环，安全")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--sources", default=",".join(ENABLED_SOURCES),
                    help="启用的数据源，逗号分隔：workbuddy,codex,claude,dsh（默认全开）")
    ap.add_argument("--ledger", action="store_true",
                    help="额外并入 HUD 账本里的非 WorkBuddy 记录（仅在客户端日志已删除时需要）")
    args = ap.parse_args()
    TOP_N = args.top
    ENABLED_SOURCES = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    USE_LEDGER = bool(args.ledger)

    roots = active_roots()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 数据源：" +
          ("、".join(f"{k}={v}" for k, v in roots.items()) or "（一个都没找到）"), flush=True)

    # 首次运行生成带占位符的配置模板（含填写说明，权限 600）；已存在则不动
    if wb_online.ensure_config():
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 已生成配置模板："
              f"{wb_online.CONFIG_PATH}（其中的 api_keys 段可直接填 Key）", flush=True)

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        # 端口被占用是最常见的启动失败：给一句能照着做的提示，而不是抛栈
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动失败：端口 {args.port} "
              f"无法使用（{e}）。请关闭占用该端口的程序，或用 --port 换一个端口。",
              flush=True)
        raise SystemExit(1)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 服务已启动: http://{args.host}:{args.port}/ "
          f"（应用页面；旧报告在 /report）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
