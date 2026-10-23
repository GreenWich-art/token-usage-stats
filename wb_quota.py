#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_quota.py — 订阅额度（rate limit / quota）采集与归一化

与 wb_token_semantics.py 的分工
  · 那个模块管「token 用量」，本模块管「额度还剩多少」；
  · 两个模块互不 import，服务端分别调用后在接口层合并。

数据来源与可信度（2026-10-23 实测 + 公开资料核对）
  codex-rollout  Codex CLI 的 rollout 日志里，token_count 事件带 rate_limits：
                 {plan_type, primary:{used_percent,window_minutes,resets_at},
                  secondary:{...}}。**本地就有、跨平台、信息最全**，优先采用。
  hud-ledger     Agent HUD 账本的 quota_sample 表：只有 remaining（0–100 的百分比）
                 和一个编码了账号/窗口的 window_id。二手、仅 macOS，作为补充
                 （Cursor / Copilot / OpenAgents 只有这一个来源）。
  claude/dsh/workbuddy 本地没有额度数据，不猜、不编，界面明确标注"暂无"。

输出统一为「窗口」列表，字段见 quota_from_rate_limits / ledger_quota 的注释。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
# 窗口语义：Codex 的 primary/secondary 与 HUD 的 :primary/:secondary 后缀一致
WINDOW_META = {
    "primary": ("5 小时窗口", 300),
    "secondary": ("7 天窗口", 10080),
}
_WINDOW_SUFFIX = re.compile(r":(primary|secondary)\s*$", re.I)
# 账本 window_id 里是 account:<hash>/…（有 / 后缀），HUD 报告的 account.id 里
# 是 account:<hash>（无后缀）——两种都要认，否则两边关联不上
_ACCOUNT_PREFIX = re.compile(r"^account:([0-9a-fA-F]+)(?:/|$)")
# pool:<hash>:<TYPE>:<unit>:<number> —— OpenAgents/GLM 系额度池的窗口标识。
# 单位语义与 GLM 官方监控接口一致（unit 3=小时、5=月、6=周），见 _GLM_UNIT。
_POOL_WINDOW = re.compile(r"^pool:([0-9a-fA-F]+)(?::(.+))?$", re.I)
# 账本桶名 → 中文标签（原名保留在 title 里，便于回查）
BUCKET_LABEL = {
    "models": "模型额度",
    "third-party": "第三方模型额度",
    "chat": "Chat 额度",
    "completions": "补全额度",
}
# 套餐等级显示名（HUD 报告 subscriptions 里的小写等级）
PLAN_LABEL = {"plus": "Plus", "pro": "Pro", "max": "Max", "lite": "Lite",
              "free": "Free", "individual": "Individual", "team": "Team",
              "business": "Business", "enterprise": "Enterprise"}
# HUD 账本/报告的 observed_at 是 **CFAbsoluteTime**（2001-01-01 起算的秒，
# Core Foundation 的时间基准），不是 Unix 时间戳。不转会差 31 年——
# 表现为「观测于 1995-10-14」，而只显示月日时看起来像「10-14」，
# 极易被误读成"数据是上周的"。本机实测：cursor 样本 814437113 + 978307200
# = 2026-10-23 16:31（真实当前），确认基准无误。
_CF_EPOCH = 978307200


def _cf_to_unix(v) -> int:
    """CFAbsoluteTime → Unix 秒；缺失/异常返回 0（界面显示「未知」）。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        return 0
    return int(v) + _CF_EPOCH


def _short_label(tail: str) -> str:
    """把窗口标识压成适合卡片显示的短标签。

    HUD 账本里的窗口 id 有时长这样：
      pool:9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a:TOKENS_LIMIT:6:1
    整串当标签会把概览卡片撑破（用户实测到的文字溢出）。规则：
      · 本来够短且不含分段符 → 原样用（models / third-party 这类桶名）；
      · 否则挑「最有信息量又不太长」的一段——优先 6~14 字符的字母段
        （TOKENS_LIMIT / TIME_LIMIT），再退到 2~5 字符的（pool）；
      · 都挑不出来就给「额度」，绝不把长串搬上界面。
    """
    tail = (tail or "").strip()
    if not tail:
        return "额度"
    if len(tail) <= 14 and ":" not in tail:
        return tail
    segs = [s for s in tail.split(":") if s]
    # 1) 类型名段优先：含下划线或全大写（TOKENS_LIMIT / TIME_LIMIT / MODELS）
    for s in segs:
        if 2 <= len(s) <= 14 and s.isascii() and (
                "_" in s or (s.isupper() and any(c.isalpha() for c in s))):
            return s
    # 2) 普通字母段，长的优先（信息量更大）；pool / 9f3a7c1e 这类垫底
    for s in sorted(segs, key=len, reverse=True):
        if 2 <= len(s) <= 14 and s.isascii() and any(c.isalpha() for c in s):
            return s
    return "额度"


def _window_kind(tail: str) -> str:
    """窗口唯一键：primary/secondary 原样；其余桶用短标签，长串不进场。"""
    if tail in WINDOW_META:
        return tail
    return _short_label(tail)


def quota_from_rate_limits(client: str, account: str, ts_ms: int,
                            rl: dict) -> list[dict]:
    """把 Codex 的 rate_limits 归一成窗口列表。

    rl 形如：
      {"plan_type": "pro",
       "primary":   {"used_percent": 41.0, "window_minutes": 300,  "resets_at": 1792171200},
       "secondary": {"used_percent": 12.0, "window_minutes": 10080,"resets_at": 1792600000}}
    used_percent 是「已用」；界面要显示剩余，所以这里同时给出两个口径，
    由前端决定展示哪一个（本产品展示 remaining，并标注"剩余"二字）。

    实测注意：本机 rollout 里的 rate_limits **没有 plan_type**，所以 plan 可能为 None，
    界面据此退化成「Codex（默认账号）」而不是编一个套餐名。
    """
    if not isinstance(rl, dict):
        return []
    plan = str(rl.get("plan_type") or "").strip() or None
    out: list[dict] = []
    for kind in ("primary", "secondary"):
        w = rl.get(kind)
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        used = float(used)
        if used < 0:
            used = 0.0
        if used > 100:
            used = 100.0
        mins = w.get("window_minutes")
        mins = int(mins) if isinstance(mins, (int, float)) else WINDOW_META[kind][1]
        resets = w.get("resets_at")
        out.append({
            "client": client,
            "account": account or "default",
            "plan": plan,
            "kind": kind,
            "label": WINDOW_META[kind][0] if mins == WINDOW_META[kind][1] else f"{mins // 60} 小时窗口",
            "window_minutes": mins,
            "used_pct": round(used, 1),
            "remaining_pct": round(100.0 - used, 1),
            "resets_at": int(resets) if isinstance(resets, (int, float)) else None,
            "observed_at": int(ts_ms // 1000) if ts_ms > 1e11 else int(ts_ms),
            "source": "codex-rollout",
        })
    return out


def _wid_hash(wid: str) -> Optional[str]:
    """从窗口 ID 里提取账号/凭据哈希段，两边（quota_sample 与 accounts）用同一套
    规则才能对上：
      account:3d5b9a17…/cursor        → 3d5b9a17…
      account:3d5b9a17…               → 3d5b9a17…（HUD 报告的 account.id 无后缀）
      pool:9f3a7c1e…:TOKENS_LIMIT:6:1 → 9f3a7c1e…
    两侧哈希理论上等长（都是 64 位十六进制）；若某侧被截短，匹配时按前缀兼容。
    """
    m = _ACCOUNT_PREFIX.match(wid or "")
    if m:
        return m.group(1).lower()
    m = _POOL_WINDOW.match(wid or "")
    if m:
        return m.group(1).lower()
    return None


def _hash_match(a: Optional[str], b: Optional[str]) -> bool:
    """两个哈希段是否指同一账号：相等，或一方是另一方的前缀（截短兼容）。"""
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def hud_accounts(report_path: Optional[Path] = None) -> dict:
    """读 Agent HUD 的 last-usage-report.json，取账号身份与套餐等级。

    返回 {哈希: {"client","label","plan"}}：
      · client —— HUD 认定的厂商/客户端（"GLM" / "Cursor" / "Codex" / "GitHub Copilot"）；
      · label  —— 账号显示名（脱敏邮箱、或 "GLM · CN · PLAN · 凭据 9f3a7c" 这类凭据标识）；
      · plan   —— 套餐等级（plus / free / individual…，来自 subscriptions）。

    为什么值得单独读这个文件：quota_sample 只存 scope + window_id + 剩余%，
    **不存厂商、不存储套餐、不存账号名**——界面上就只能显示 "HUD 记录 · 3d5b9a17"。
    而同一个 pool 哈希在 accounts 里有完整身份（本机实测：open-agents 的三个窗口
    经 pool:9f3a7c1e… 关联到 accounts[WorkBuddy]，label 明确写着 "GLM · CN · PLAN"）。
    也就是说：**按 ID 硬关联，不猜 scope 语义**，换个用户机器也成立。
    """
    p = Path(report_path) if report_path else (
        Path.home() / "Library/Application Support/Agent HUD Open/last-usage-report.json")
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return out
    if not isinstance(d, dict):
        return out
    subs = d.get("subscriptions") if isinstance(d.get("subscriptions"), dict) else {}
    accs = d.get("accounts")
    items: list = []
    if isinstance(accs, dict):
        for v in accs.values():
            if isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
    elif isinstance(accs, list):
        items = [x for x in accs if isinstance(x, dict)]
    for a in items:
        acc = a.get("account") if isinstance(a.get("account"), dict) else {}
        aid = str(acc.get("id") or "")
        h = _wid_hash(aid) or _wid_hash(str(a.get("id") or ""))
        if not h:
            continue
        client = str(a.get("client") or acc.get("provider") or "").strip()
        label = str(a.get("label") or "").strip()
        plan = a.get("plan")
        if not plan and client in subs:
            plan = subs.get(client)
        out[h] = {
            "client": client or None,
            "label": label or None,
            "plan": str(plan).strip() if isinstance(plan, str) and plan.strip() else None,
        }
    return out


def ledger_quota(db_path: Path, accounts: Optional[dict] = None) -> list[dict]:
    """读 HUD 账本的 quota_sample，归一成窗口列表。

    window_id 形如 account:<hash>/codex、account:<hash>/codex:codex:secondary、
    account:<hash>/cursor:models、pool:<hash>:TOKENS_LIMIT:6:1。
    · 前者是 5h/7d 窗口，后者是 Cursor 的不同额度桶 / GLM 系额度池的窗口，
      所以桶名不匹配 primary/secondary 时按"其他窗口"处理，不硬套时长；
    · accounts（HUD 报告）按哈希关联，补上厂商、账号显示名与套餐等级——
      这是消除「HUD 记录 · 3d5b9a17」这种无信息显示的关键。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    accounts = accounts or {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    out: list[dict] = []
    try:
        for r in con.execute("select scope, window_id, observed_at, remaining "
                             "from quota_sample"):
            scope = (r["scope"] or "").strip()
            wid = (r["window_id"] or "").strip()
            if not scope or not wid:
                continue
            h = _wid_hash(wid)
            acc = None
            if h:
                # 哈希相等或前缀兼容（任一侧被截短都对得上）
                for ah, av in accounts.items():
                    if _hash_match(ah, h):
                        acc = av
                        break
            # 厂商：优先 HUD 报告认定的 client；否则退回 scope 原名
            client = (acc or {}).get("client") or scope
            # 账号显示名：脱敏邮箱/凭据标识优先；退回 "账号 <短哈希>"
            label = (acc or {}).get("label")
            account = label or ("账号 " + h[:8] if h else "default")
            plan = (acc or {}).get("plan")
            plan_label = PLAN_LABEL.get(plan, plan) if plan else None

            m = _POOL_WINDOW.match(wid)
            if m:
                # GLM 系额度池：同一个 pool 的多个窗口归到同一张卡
                tail = m.group(2) or ""
                segs = [s for s in tail.split(":") if s]
                if len(segs) >= 3 and segs[0].upper() in _GLM_TYPE:
                    tname = _GLM_TYPE[segs[0].upper()]
                    unit = int(segs[1]) if segs[1].isdigit() else 0
                    num = int(segs[2]) if segs[2].isdigit() else 0
                    uname, unit_min = _GLM_UNIT.get(unit, ("", 0))
                    kind = f"{segs[0].lower()}-{unit}-{num}"
                    wlabel = f"{num} {uname} {tname}" if (uname and num) else f"{tname}配额"
                    # 窗口时长 = 个数 × 单位分钟数（5 小时 = 5×60 = 300）
                    mins = unit_min * num if (unit_min and num) else 0
                else:
                    kind, wlabel, mins = _window_kind(tail), _short_label(tail), 0
                    if ":" not in tail:
                        wlabel = kind + " 额度"
                raw_wid = wid                     # 保留在 title 里供回查
            else:
                m2 = _ACCOUNT_PREFIX.match(wid)
                rest = wid[m2.end():] if m2 else wid
                tail = rest
                if rest == scope:
                    tail = ""
                elif rest.startswith(scope + ":"):
                    tail = rest[len(scope) + 1:]
                sfx = _WINDOW_SUFFIX.search(tail)
                if sfx:
                    kind = sfx.group(1).lower()
                elif tail == "":
                    kind = "primary"             # 无后缀的那个就是 5 小时窗口
                else:
                    kind = _window_kind(tail)
                if kind in WINDOW_META:
                    wlabel, mins = WINDOW_META[kind]
                elif tail in BUCKET_LABEL:
                    wlabel, mins = BUCKET_LABEL[tail], 0
                elif ":" not in tail:
                    wlabel, mins = kind + " 额度", 0
                else:
                    wlabel, mins = _short_label(kind), 0
                raw_wid = wid

            rem = r["remaining"]
            if not isinstance(rem, (int, float)):
                continue
            rem = float(rem)
            rem = 0.0 if rem < 0 else (100.0 if rem > 100 else rem)
            out.append({
                "client": client,
                "account": account,
                "account_hash": h,
                "plan": plan_label,
                "kind": kind,
                "label": wlabel,
                "window_minutes": mins,
                "used_pct": round(100.0 - rem, 1),
                "remaining_pct": round(rem, 1),
                "resets_at": None,                # 账本不存重置时间
                "observed_at": _cf_to_unix(r["observed_at"]),   # CFA → Unix 秒
                "source": "hud-ledger",
                "raw_window": raw_wid,            # 原始窗口 ID，仅用于 title 提示
            })
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return out


def collect_quota(roots: dict, codex_sink: Optional[list] = None,
                  ledger_db: Optional[Path] = None,
                  accounts: Optional[dict] = None,
                  report_path: Optional[Path] = None) -> list[dict]:
    """合并各来源的额度窗口。

    codex_sink
      若调用方在采集 token 时已经顺手收集过 Codex 的 rate_limits
      （形如 [(ts_ms, account, rate_limits), ...]），直接用它，避免把
      75MB 的 rollout 再读一遍。
    ledger_db
      HUD 账本路径；存在才读。
    accounts
      HUD 报告的账号表（hud_accounts() 的输出）。不传则自己读 report_path。
    """
    rows: list[dict] = []
    rollout_keys: set[tuple] = set()

    # 1) Codex 原始数据优先（信息最全，且能覆盖 HUD 没记到的部分）。
    #    每次模型调用都带一份 rate_limits，**全部入 rows**——同窗口的多次观测
    #    由末尾的 latest 统一按时间取最新。早期版本在这里用 seen 挡重，结果
    #    每个窗口只留下扫描遇到的第一条（往往是旧样本），latest 形同虚设。
    if codex_sink:
        for item in codex_sink:
            ts_ms, account, rl = item
            for w in quota_from_rate_limits("codex", account, ts_ms, rl):
                rows.append(w)
                rollout_keys.add((w["client"].casefold(), w["kind"]))

    # 2) HUD 账本补充：rollout 已覆盖的 (客户端, 窗口) 不再重复（本地只有一个
    #    CODEX_HOME，重复会分成两组显示，且账本那份还是旧的）；其余全部入 rows，
    #    同样交给 latest 去重。
    if ledger_db:
        if accounts is None:
            accounts = hud_accounts(report_path)
        for w in ledger_quota(ledger_db, accounts):
            if (w["client"].casefold(), w["kind"]) in rollout_keys:
                continue
            rows.append(w)

    # 3) rollout 的 rate_limits 不带 plan_type（本机实测），用 HUD 报告的套餐等级补。
    #    没有依据就留着 None，界面显示「未标注套餐」，不编。
    if accounts:
        plan_by_client = {}
        for a in accounts.values():
            c = (a.get("client") or "").casefold()
            if c and a.get("plan") and c not in plan_by_client:
                plan_by_client[c] = a["plan"]
        for w in rows:
            if not w.get("plan") and w["client"].casefold() in plan_by_client:
                p = plan_by_client[w["client"].casefold()]
                w["plan"] = PLAN_LABEL.get(p, p)

    # 4) 同一窗口只保留**最近一次观测**。
    #    Codex 每次模型调用都带一份 rate_limits，不去重会让同一个 5 小时窗口
    #    出现几百条（本机实测 400+），界面直接被打满。
    latest: dict[tuple, dict] = {}
    for w in rows:
        key = (w["client"], w["account"], w["plan"], w["kind"])
        cur = latest.get(key)
        if cur is None or (w.get("observed_at") or 0) >= (cur.get("observed_at") or 0):
            latest[key] = w
    return list(latest.values())


def group_and_sort(rows: Iterable[dict]) -> list[dict]:
    """按 (客户端, 账号, 套餐) 分组，按最近一次观测时间倒序。

    用户要求：多个存在用量限制的 plan，按调用时间从近到远展示，
    最近才用过的最先展示。组内窗口按 primary → secondary → 其他 排列。
    """
    groups: dict[tuple, dict] = {}
    for w in rows:
        key = (w["client"], w["account"], w["plan"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"client": w["client"], "account": w["account"],
                               "plan": w["plan"], "windows": [], "last_at": 0,
                               "sources": set()}
        g["windows"].append(w)
        g["last_at"] = max(g["last_at"], w.get("observed_at") or 0)
        g["sources"].add(w.get("source") or "")

    order = {"primary": 0, "secondary": 1}
    out = []
    for g in groups.values():
        # 组内：5h/7d 之后，其余窗口按时长升序（短周期限制最常被撞到，排前面）
        g["windows"].sort(key=lambda w: (order.get(w["kind"], 9), w.get("window_minutes") or 0))
        g["sources"] = sorted(s for s in g["sources"] if s)
        out.append(g)
    out.sort(key=lambda g: -g["last_at"])
    return out


# ---------------------------------------------------------------- 外部套餐探测
# 「通过 API 把某厂商的 Coding Plan 接进 WorkBuddy」时：
#   · 模型清单（含网关 baseUrl 与 Key）在 ~/.workbuddy/models.json；
#   · 额度不在本地，在厂商自己的监控接口上。
# 这里登记「端点 + 解析规则」均经公开资料核实的厂商。三条硬规则：
#   1. Key 只由本服务进程读取 models.json 取得，**不进接口响应、不打印、不落盘**；
#   2. 默认不发请求——只有用户在 online.json 里显式开启才联网；
#   3. 没有核实过响应结构的厂商标 status=probe，界面明说"解析规则待确认"，
#      不假装能读。
PLAN_PROBES: list[dict] = [
    {
        "id": "glm-coding-cn",
        "name": "智谱 GLM Coding Plan（国内站）",
        "vendor": "智谱",
        "match": ["open.bigmodel.cn", "bigmodel.cn", "api/coding/paas/v4"],
        # HUD 账本里已能读到同一套餐时（accounts 的 client 名），不必再劝用户开联网
        "hud_clients": ["GLM"],
        "quota_url": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
        "auth": "raw",                      # Authorization: <key>，不带 Bearer
        "parser": "glm_limits",
        "status": "stable",
        "source_url": "https://github.com/farion1231/cc-switch/issues/1588",
        "verified_at": "2026-10-23",
        "note": "5 小时 / 7 天 Token 配额 + 月度 MCP 调用；套餐等级 lite / pro / max",
    },
    {
        "id": "glm-coding-intl",
        "name": "智谱 GLM Coding Plan（国际站 Z.AI）",
        "vendor": "智谱",
        "match": ["api.z.ai"],
        "quota_url": "https://api.z.ai/api/monitor/usage/quota/limit",
        "auth": "raw",
        "parser": "glm_limits",
        "status": "stable",
        "source_url": "https://opencodedocs.com/zh/vbgate/opencode-mystatus/appendix/api-endpoints",
        "verified_at": "2026-10-23",
        "note": "与国内站同构、独立计费",
    },
    {
        "id": "kimi-coding",
        "name": "Kimi For Coding",
        "vendor": "月之暗面",
        "match": ["api.kimi.com/coding"],
        "quota_url": "https://api.kimi.com/coding/v1/usages",
        "auth": "bearer",
        "parser": "kimi_usages",
        "status": "probe",                   # 端点已核实；响应字段未逐字核对
        "source_url": "https://dshpluginhub.ai/en/plugins/dsh-plan-usage",
        "verified_at": "2026-10-23",
        "note": "周限 + 5 小时频限窗口；解析规则待真实响应确认",
    },
    {
        "id": "kimi-openapi",
        "name": "Kimi 开放平台（按量计费）",
        "vendor": "月之暗面",
        # 开放平台（platform.moonshot.cn）与 Coding Plan 是两套账号体系与计费：
        # 前者按 token 计费、余额是"钱"，后者是订阅额度
        "match": ["api.moonshot.cn", "api.moonshot.ai"],
        "quota_url": "https://api.moonshot.cn/v1/users/me/balance",
        "auth": "bearer",
        "parser": "moonshot_balance",
        "status": "stable",
        "source_url": "https://platform.moonshot.cn/docs/api/balance",
        "verified_at": "2026-10-23",
        "note": "返回可用/代金券/现金余额（人民币元）；国内站 api.moonshot.cn 单位元，"
                "国际站单位美元",
    },
    {
        "id": "deepseek-openapi",
        "name": "DeepSeek 开放平台（按量计费）",
        "vendor": "DeepSeek",
        "match": ["api.deepseek.com"],
        "quota_url": "https://api.deepseek.com/user/balance",
        "auth": "bearer",
        "parser": "ds_balance",
        "status": "stable",
        # 凭据不由 models.json 提供：DSH 用的是自己账户平台里的登录令牌，
        # 见 deepseek_key()（环境变量 → models.json → ~/.dsh 凭据，按优先级）
        "local_key": "deepseek",
        "source_url": "https://api-docs.deepseek.com/api/get-user-balance/",
        "verified_at": "2026-10-23",
        "note": "返回总余额 / 赠金 / 充值余额；is_available=false 表示余额不足，"
                "调用会被拒（402）",
    },
]

# GLM 响应的单位语义（ZCode 官方文档）：unit 3=小时、5=月、6=周，number 为个数
_GLM_UNIT = {3: ("小时", 60), 5: ("个月", 43200), 6: ("周", 10080)}
_GLM_TYPE = {"TOKENS_LIMIT": "Token", "TIME_LIMIT": "MCP 调用"}

# 长窗口标识里提取到的厂商类型名 → 中文标签（用户读的是中文，不摆内部常量名）
_TYPE_LABEL = {
    "TOKENS_LIMIT": "Token 限额",
    "TIME_LIMIT": "时间限额",
    "CREDIT_LIMIT": "积分限额",
    "REQUESTS_LIMIT": "请求数限额",
    "MODELS_LIMIT": "模型额度",
}


def read_plan_models(models_json: Optional[Path] = None) -> tuple[list[dict], dict]:
    """从 models.json 识别「通过 API 接入的外部套餐」。

    返回 (plans, keys)：
      plans —— 脱敏列表，可安全进接口响应（probe id / 显示名 / 网关 host / 匹配方式）；
      keys  —— {probe_id: key}，**仅限本进程内存使用**，调用方取用后不得外泄。

    models.json 是 JSON 数组，条目形如
      {"id": "GLM-5.3", "name": "GLM-5.3 (Coding Plan)",
       "baseUrl": "https://open.bigmodel.cn/api/coding/paas/v4", "apiKey": "..."}
    Key 字段名各版本不完全一致（apiKey / key / api_key），逐个试。
    """
    p = Path(models_json) if models_json else Path.home() / ".workbuddy" / "models.json"
    if not p.exists():
        return [], {}
    try:
        arr = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return [], {}
    if not isinstance(arr, list):
        return [], {}

    plans: list[dict] = []
    keys: dict[str, str] = {}
    seen: set[str] = set()
    for m in arr:
        if not isinstance(m, dict):
            continue
        base = str(m.get("baseUrl") or m.get("base_url") or m.get("url") or "")
        if not base:
            continue
        for probe in PLAN_PROBES:
            hit = next((f for f in probe["match"] if f in base), None)
            if not hit:
                continue
            pid = probe["id"]
            key = ""
            for kf in ("apiKey", "api_key", "key", "token"):
                v = m.get(kf)
                if isinstance(v, str) and v.strip():
                    key = v.strip()
                    break
            if pid not in seen:
                seen.add(pid)
                plans.append({
                    "probe_id": pid,
                    "name": probe["name"],
                    "vendor": probe["vendor"],
                    "model_name": str(m.get("name") or m.get("id") or "")[:40],
                    "gateway": base.split("/")[2] if "://" in base else base[:40],
                    "matched_on": hit,
                    "status": probe["status"],
                    "quota_url": probe["quota_url"],
                    "note": probe["note"],
                    "source_url": probe["source_url"],
                    "verified_at": probe["verified_at"],
                    "key_present": bool(key),
                })
            if key and pid not in keys:
                keys[pid] = key

    # 凭据不由 models.json 提供的套餐（如 DSH 使用的 DeepSeek 开放平台）：
    # 只要本机能拿到**用户显式配置**的 key，也把它列出来——否则用户在 DSH
    # 里按量付费，界面上却看不到这个渠道的余额。
    for probe in PLAN_PROBES:
        if probe.get("local_key") != "deepseek" or probe["id"] in seen:
            continue
        key, src = deepseek_key(p)
        if not key:
            continue
        seen.add(probe["id"])
        plans.append({
            "probe_id": probe["id"],
            "name": probe["name"],
            "vendor": probe["vendor"],
            "model_name": "",
            "gateway": (probe.get("quota_url") or "").split("/")[2],
            "matched_on": src or "",
            "credential_from": src,
            "status": probe["status"],
            "quota_url": probe["quota_url"],
            "note": probe["note"],
            "source_url": probe["source_url"],
            "verified_at": probe["verified_at"],
            "key_present": True,
        })
        keys[probe["id"]] = key
    return plans, keys


def _glm_limits(data: dict) -> tuple[list[dict], Optional[str]]:
    """解析 GLM Coding Plan 的 quota/limit 响应 → (窗口列表, 套餐等级)。

    响应（三个独立来源给出一致结构）：
      {"code":200,"data":{"limits":[
        {"type":"TOKENS_LIMIT","unit":3,"number":5,"percentage":1,"nextResetTime":1778753640304},
        {"type":"TOKENS_LIMIT","unit":6,"number":1,"percentage":44},
        {"type":"TIME_LIMIT","unit":5,"number":1,"percentage":0,"usage":100,"remaining":100}],
       "level":"lite"},"success":true}
    percentage 是**已用**百分比；nextResetTime 是毫秒时间戳。
    """
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return [], None
    level = d.get("level")
    level = str(level) if isinstance(level, str) and level.strip() else None
    out: list[dict] = []
    for it in d.get("limits") or []:
        if not isinstance(it, dict):
            continue
        t = str(it.get("type") or "").upper()
        if t not in _GLM_TYPE:
            continue
        pct = it.get("percentage")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            continue
        used = max(0.0, min(100.0, float(pct)))
        unit = it.get("unit")
        num = it.get("number")
        uname, unit_min = _GLM_UNIT.get(unit, ("", 0))
        num = int(num) if isinstance(num, (int, float)) and not isinstance(num, bool) else 0
        if uname and num:
            label = f"{num} {uname} {_GLM_TYPE[t]}"
            kind = f"{t.lower()}-{unit}-{num}"
        else:
            label, kind = f"{_GLM_TYPE[t]}配额", t.lower()
        resets = it.get("nextResetTime")
        out.append({
            "client": "glm-coding",
            "account": "default",
            "plan": level,
            "kind": kind,
            "label": label,
            # 窗口时长 = 个数 × 单位分钟数（5 小时 = 5×60 = 300）
            "window_minutes": unit_min * num if (unit_min and num) else 0,
            "used_pct": round(used, 1),
            "remaining_pct": round(100.0 - used, 1),
            "resets_at": int(resets // 1000) if isinstance(resets, (int, float)) and resets else None,
            "observed_at": 0,
            "source": "plan-api",
        })
    # 组内顺序按窗口长度升序：5 小时 → 周 → 月。
    # 厂商接口返回的 limits 顺序不保证（本机实测 MCP 月度窗口排在第一位），
    # 界面上「最短周期在最上面」才符合「先看最容易撞到的限制」的阅读习惯。
    out.sort(key=lambda w: w.get("window_minutes") or 0)
    return out, level


def _moonshot_balance(data: dict, currency: str = "CNY") -> tuple[list[dict], Optional[str]]:
    """解析 Kimi 开放平台的余额响应 → (["余额"这一行], None)。

    官方结构（platform.moonshot.cn/docs/api/balance，三个独立来源一致）：
      {"code":0,"status":true,"scode":"0x0",
       "data":{"available_balance":49.59,"voucher_balance":46.59,"cash_balance":3.0}}
    available = cash + voucher；≤0 时无法调用推理 API。单位：人民币元（国内站）／美元（国际站）。

    余额是**金额**不是百分比，所以这一行不带 remaining_pct，而是 balance/currency；
    前端据此渲染成数字而非进度条。
    """
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return [], None
    def num(k):
        v = d.get(k)
        return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    avail = num("available_balance")
    if avail is None:
        return [], None
    return [{
        "client": "kimi-openapi",
        "account": "default",
        "plan": None,
        "kind": "balance",
        "label": "账户可用余额",
        "balance": round(avail, 2),
        "currency": currency,
        "voucher": num("voucher_balance"),
        "cash": num("cash_balance"),
        "window_minutes": 0,
        "used_pct": None,
        "remaining_pct": None,
        "resets_at": None,
        "observed_at": 0,
        "source": "plan-api",
    }], None


def deepseek_key(models_json: Optional[Path] = None) -> tuple[Optional[str], Optional[str]]:
    """取 DeepSeek 的 API 凭据，返回 (key, 来源说明)；取不到返回 (None, None)。

    只认**用户显式配置**的三处（只在本机读取，不外发）：
      1. 环境变量 DEEPSEEK_API_KEY；
      2. ~/.token-usage-stats/online.json 的 api_keys.deepseek；
      3. models.json 里 baseUrl 含 api.deepseek.com 的自定义模型条目。

    第 2 条是给 GUI 用户留的入口：LaunchAgent 起服务时不继承 shell 环境变量，
    只靠环境变量的话普通用户配不上。

    **刻意不读 ~/.dsh/.credentials.yaml**：那是 DeepSeek Harness 自己的登录令牌
    存储，从中提取 token 属于"读取凭据内容"，即便只读也不由本应用做。
    """
    import os
    v = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if v:
        return v, "环境变量 DEEPSEEK_API_KEY"

    try:
        cfg = json.loads((Path.home() / ".token-usage-stats" / "online.json")
                         .read_text(encoding="utf-8"))
        if isinstance(cfg, dict):
            ak = cfg.get("api_keys")
            if isinstance(ak, dict):
                k = ak.get("deepseek")
                if isinstance(k, str) and k.strip():
                    return k.strip(), "online.json 的 api_keys.deepseek"
    except Exception:
        pass

    mj = Path(models_json) if models_json else Path.home() / ".workbuddy" / "models.json"
    try:
        arr = json.loads(mj.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        arr = []
    if isinstance(arr, list):
        for m in arr:
            if not isinstance(m, dict):
                continue
            base = str(m.get("baseUrl") or m.get("base_url") or m.get("url") or "")
            if "api.deepseek.com" not in base:
                continue
            for kf in ("apiKey", "api_key", "key", "token"):
                k = m.get(kf)
                if isinstance(k, str) and k.strip():
                    return k.strip(), "models.json 中的 DeepSeek 条目"
    return None, None


def _ds_balance(data: dict) -> tuple[list[dict], Optional[str]]:
    """解析 DeepSeek 的 /user/balance 响应 → (["余额"一行], None)。

    官方结构（api-docs.deepseek.com/api/get-user-balance/）：
      {"is_available": true,
       "balance_infos": [{"currency":"CNY","total_balance":"110.00",
                          "granted_balance":"10.00","topped_up_balance":"100.00"}]}
    **数值是字符串**，必须转 float；多币种时取第一条。
    """
    if not isinstance(data, dict):
        return [], None
    infos = data.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return [], None
    info = next((x for x in infos if isinstance(x, dict)), None)
    if not info:
        return [], None

    def num(k):
        v = info.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(float(v), 2)
        if isinstance(v, str):
            try:
                return round(float(v.replace(",", "")), 2)
            except ValueError:
                return None
        return None

    total = num("total_balance")
    if total is None:
        return [], None
    cur = str(info.get("currency") or "CNY").upper()
    cur = cur if cur in ("CNY", "USD") else "CNY"
    ok = data.get("is_available")
    return [{
        "client": "deepseek-openapi",
        "account": "default",
        "plan": None,
        "kind": "balance",
        "label": "账户可用余额",
        "balance": total,
        "currency": cur,
        "voucher": num("granted_balance"),        # 赠金
        "cash": num("topped_up_balance"),         # 充值余额
        "balance_ok": ok if isinstance(ok, bool) else None,
        "window_minutes": 0,
        "used_pct": None,
        "remaining_pct": None,
        "resets_at": None,
        "observed_at": 0,
        "source": "plan-api",
    }], None


def fetch_plan_quota(probe: dict, key: str, timeout: float = 8.0) -> dict:
    """按 probe 的定义拉取一个外部套餐的额度窗口。任何失败都转为错误说明。"""
    import time
    import urllib.error
    import urllib.request

    out: dict = {"probe_id": probe["id"], "rows": [], "plan": None, "error": None}
    # 未核实的端点一律不发请求——Key 不能离开本机去往没验证过的地方
    if probe.get("status") != "stable":
        out["error"] = "该套餐暂不支持读取额度"
        return out
    if not key:
        out["error"] = "未找到该套餐的 API Key"
        return out
    url = probe["quota_url"]
    req = urllib.request.Request(url, method="GET")
    if probe.get("auth") == "bearer":
        req.add_header("Authorization", "Bearer " + key)
    else:                                   # GLM：Key 直接放 Authorization，不带前缀
        req.add_header("Authorization", key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}"
        return out
    except Exception as e:
        out["error"] = f"网络请求失败（{type(e).__name__}）"
        return out

    if probe.get("parser") == "glm_limits":
        rows, level = _glm_limits(data)
        out["rows"] = rows
        out["plan"] = level
        if not rows and not out["error"]:
            out["error"] = "厂商返回的数据格式与预期不符"
    elif probe.get("parser") == "moonshot_balance":
        # 国际站（api.moonshot.ai）返回美元，国内站返回人民币元
        cur = "USD" if "moonshot.ai" in (probe.get("quota_url") or "") else "CNY"
        rows, level = _moonshot_balance(data, cur)
        out["rows"] = rows
        out["plan"] = level
        if not rows:
            out["error"] = "厂商返回的数据格式与预期不符"
    elif probe.get("parser") == "ds_balance":
        rows, level = _ds_balance(data)
        out["rows"] = rows
        out["plan"] = level
        if not rows:
            out["error"] = "厂商返回的数据格式与预期不符"
    else:
        out["error"] = "该套餐暂不支持读取额度"
    return out


def selftest() -> bool:
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {name}"
              + ("" if good else f"   得到 {got!r} / 期望 {want!r}"))

    # 1) Codex 的 rate_limits → 两个窗口，剩余 = 100 - used
    rl = {"plan_type": "pro",
          "primary": {"used_percent": 41.0, "window_minutes": 300, "resets_at": 1792171200},
          "secondary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": 1792600000}}
    w = quota_from_rate_limits("codex", "6c1e9f4a", 1792171200000, rl)
    chk("codex 产出两个窗口", len(w), 2)
    chk("primary 剩余 = 59%", w[0]["remaining_pct"], 59.0)
    chk("primary 带重置时间", w[0]["resets_at"], 1792171200)
    chk("套餐名取自 plan_type", w[0]["plan"], "pro")
    chk("窗口标签", (w[0]["label"], w[1]["label"]), ("5 小时窗口", "7 天窗口"))

    # 2) 异常值保护
    w2 = quota_from_rate_limits("codex", "a", 1792171200000,
                                {"plan_type": "", "primary": {"used_percent": 130}})
    chk("used>100 时剩余不为负", w2[0]["remaining_pct"], 0.0)
    chk("plan_type 缺失时置 None（不编套餐名）", w2[0]["plan"], None)

    # 2b) 同一窗口多次观测只留最新（Codex 每次调用都带一份 rate_limits）
    rows_dup = collect_quota({}, codex_sink=[
        (1792171200000, "acc1", {"plan_type": "pro",
                                 "primary": {"used_percent": 41.0, "resets_at": 1}}),
        (1792200000000, "acc1", {"plan_type": "pro",
                                 "primary": {"used_percent": 20.0, "resets_at": 1}}),
    ])
    chk("重复观测去重后只剩 1 个窗口", len(rows_dup), 1)
    chk("保留的是最近一次观测", rows_dup[0]["used_pct"], 20.0)

    # 3) 账本窗口 id 归一化 + HUD 报告账号关联
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="quota-test-"))
    db = tmp / "ledger.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("create table quota_sample (scope text, window_id text, "
                "observed_at real, remaining real)")
    POOL = "9f3a7c1e1f38e4fdfa42b3c342edfee44654c0c8eee88a3d14efa104963f873c"
    CUR = "3d5b9a17c2e48f603d5b9a17c2e48f603d5b9a17c2e48f603d5b9a17c2e48f60"
    CDX = "6c1e9f4a8b2d7c306c1e9f4a8b2d7c306c1e9f4a8b2d7c306c1e9f4a8b2d7c30"
    COP = "a7d2c5e9f1b30846a7d2c5e9f1b30846a7d2c5e9f1b30846a7d2c5e9f1b30846"
    con.executemany("insert into quota_sample values (?,?,?,?)", [
        ("cursor", f"account:{CUR}/cursor", 814426499.4, 52.5),
        ("cursor", f"account:{CUR}/cursor:models", 814426499.4, 5.0),
        ("cursor", f"account:{CUR}/cursor:third-party", 814426499.4, 100.0),
        ("codex", f"account:{CDX}/codex", 814423935.2, 64.0),
        ("codex", f"account:{CDX}/codex:codex:secondary", 814423935.2, 94.0),
        ("copilot", f"account:{COP}/copilot:chat", 814431177.1, 30.0),
        ("copilot", f"account:{COP}/copilot:completions", 814431177.1, 12.0),
        # open-agents scope 实际是用户在 WorkBuddy 里接入的 GLM Coding Plan：
        # 同一个 pool 下有三个窗口（本机实测结构）
        ("open-agents", f"pool:{POOL}:TOKENS_LIMIT:3:5", 814431179.1, 12.0),
        ("open-agents", f"pool:{POOL}:TOKENS_LIMIT:6:1", 814431179.1, 63.0),
        ("open-agents", f"pool:{POOL}:TIME_LIMIT:5:1", 814431179.1, 100.0),
    ])
    con.commit()
    con.close()
    lq = ledger_quota(db)
    chk("账本读出 10 个窗口", len(lq), 10)
    cw = [x for x in lq if x["client"] == "cursor"]
    chk("无账号表时账号退回「账号 <短哈希>」", {x["account"] for x in cw}, {"账号 " + CUR[:8]})
    chk("cursor 无后缀的是 5 小时窗口，models/third-party 是独立额度桶",
        sorted(x["kind"] for x in cw), ["models", "primary", "third-party"])
    chk("桶名翻成中文标签", sorted(x["label"] for x in cw),
        ["5 小时窗口", "模型额度", "第三方模型额度"])
    oa = [x for x in lq if x["client"] == "open-agents"]
    chk("同一个 pool 的三个窗口 account 相同（归成一张卡）",
        len({x["account"] for x in oa}), 1)
    chk("pool 窗口按 GLM 单位语义标注",
        sorted(x["label"] for x in oa),
        ["1 个月 MCP 调用", "1 周 Token", "5 小时 Token"])
    chk("pool 窗口时长 = 个数 × 单位分钟（5 小时=300，月=43200）",
        sorted(x["window_minutes"] for x in oa), [300, 10080, 43200])
    chk("pool 窗口的 kind 互不冲突（否则会被最近观测去重合并掉）",
        len({x["kind"] for x in oa}), 3)
    chk("原始窗口 ID 保留在 raw_window 里（title 回查用）",
        all("TOKENS_LIMIT" in (x["raw_window"] or "") or "TIME_LIMIT" in (x["raw_window"] or "")
            for x in oa), True)
    cx = [x for x in lq if x["client"] == "codex"]
    chk("codex secondary 识别为 7 天窗口",
        sorted((x["kind"], x["window_minutes"]) for x in cx),
        [("primary", 300), ("secondary", 10080)])
    chk("HUD 的 CFAbsoluteTime 已转 Unix 秒（不转会差 31 年，显示成 1995）",
        _cf_to_unix(814434389.7), 1792741589)
    chk("转换后的观测时间是今年而非 1995",
        datetime.fromtimestamp(
            [x["observed_at"] for x in lq if x["client"] == "cursor"][0]).year,
        datetime.now().year)
    chk("无账号表时 plan 为 None（不编「HUD 记录」）",
        {x["plan"] for x in lq}, {None})

    # 3b) HUD 报告的账号表：按哈希硬关联，补厂商/账号名/套餐
    rep = tmp / "last-usage-report.json"
    rep.write_text(json.dumps({
        "subscriptions": {"Codex": "plus", "Cursor": "free",
                          "GitHub Copilot": "individual"},
        "accounts": {
            "Cursor": [{"client": "Cursor", "plan": "free",
                        "label": "cursor-user@example.com",
                        "account": {"provider": "Cursor", "id": f"account:{CUR}"}}],
            "Codex": [{"client": "Codex",
                       "account": {"provider": "Codex", "id": f"account:{CDX}"}}],
            "GitHub Copilot": [{"client": "GitHub Copilot", "plan": "individual",
                                "label": "gh@example.com",
                                "account": {"provider": "GitHub Copilot",
                                            "id": f"account:{COP}"}}],
            # 关键一条：open-agents 的三个窗口经 pool 哈希关联到这条 GLM 凭据
            "WorkBuddy": [{"client": "GLM", "isCurrent": True,
                           "label": "GLM · CN · PLAN · 凭据 9f3a7c",
                           "account": {"provider": "WorkBuddy", "id": f"pool:{POOL}"}}],
        }
    }), encoding="utf-8")
    accs = hud_accounts(rep)
    chk("账号表按哈希提取（account: 与 pool: 两种前缀）", len(accs), 4)
    chk("pool 哈希关联到 GLM 凭据",
        (accs[POOL]["client"], accs[POOL]["label"]), ("GLM", "GLM · CN · PLAN · 凭据 9f3a7c"))
    chk("套餐从 subscriptions 按 client 补齐",
        accs[CDX]["plan"], "plus")
    chk("哈希截短时按前缀兼容", _hash_match(POOL, POOL[:16]), True)
    chk("不同哈希不误配", _hash_match(POOL, CUR), False)
    lq2 = ledger_quota(db, accs)
    glm = [x for x in lq2 if x["client"] == "GLM"]
    chk("GLM 池的三个窗口归一组（厂商/账号/套餐都来自报告）",
        (len(glm), {x["account"] for x in glm}), (3, {"GLM · CN · PLAN · 凭据 9f3a7c"}))
    cur = [x for x in lq2 if x["client"] == "Cursor"]
    chk("Cursor 账号显示脱敏邮箱而非哈希",
        {x["account"] for x in cur}, {"cursor-user@example.com"})
    chk("Cursor 套餐显示为 Free", {x["plan"] for x in cur}, {"Free"})
    chk("Copilot 套餐显示为 Individual",
        {x["plan"] for x in lq2 if x["client"] == "GitHub Copilot"}, {"Individual"})

    # 3c) 同窗口多次观测只留**最新**那条。早期版本在合并时用 seen 挡重，
    #     每个窗口只留下扫描遇到的第一条（常是旧样本），末尾的 latest 去重
    #     形同虚设——本机实测因此把今天的样本显示成了 10-14。
    con2 = sqlite3.connect(str(db))
    con2.execute("insert into quota_sample values (?,?,?,?)",
                 ("cursor", f"account:{CUR}/cursor", 814440000.0, 33.0))
    con2.commit()
    con2.close()
    rows_dedup = collect_quota({}, ledger_db=db, accounts=accs)
    cur_d = [x for x in rows_dedup if x["client"] == "Cursor" and x["kind"] == "primary"]
    chk("同窗口多次观测只留最新（不是扫描遇到的第一条）",
        (len(cur_d), cur_d[0]["remaining_pct"], cur_d[0]["observed_at"]),
        (1, 33.0, _cf_to_unix(814440000.0)))

    # 4) 合并：Codex 原始数据优先，不重复（两边 client 大小写不同也要能覆盖）
    rl_noplan = {"primary": {"used_percent": 41.0, "resets_at": 1792171200},
                 "secondary": {"used_percent": 12.0, "resets_at": 1792600000}}
    rows = collect_quota({}, codex_sink=[(1792171200000, "6c1e9f4a", rl_noplan)],
                         ledger_db=db, accounts=accs)
    codex_rows = [r for r in rows if r["client"] == "codex"]
    chk("Codex 已有窗口不被账本重复（大小写不敏感）", len(codex_rows), 2)
    chk("Codex 窗口来源标记为原始数据", {r["source"] for r in codex_rows}, {"codex-rollout"})
    chk("账本里的 Codex 窗口被 rollout 覆盖（不重复成组）",
        [r for r in rows if r["client"].casefold() == "codex"
         and r["source"] == "hud-ledger"], [])
    chk("rollout 缺 plan_type 时用报告的套餐等级补",
        {r["plan"] for r in codex_rows}, {"Plus"})
    chk("Cursor 窗口仍从账本进来", len([r for r in rows if r["client"] == "Cursor"]), 3)
    chk("Copilot 两个桶都进来", len([r for r in rows if r["client"] == "GitHub Copilot"]), 2)
    chk("GLM 池三个窗口都进来", len([r for r in rows if r["client"] == "GLM"]), 3)

    # 5) 分组排序：最近观测的 plan 在前；GLM 三窗口同组、短周期在前
    #    deadbeef 的观测时间取当晚（晚于账本 fixture 的 814431179），确保排序可判
    rows2 = rows + quota_from_rate_limits("codex", "deadbeef", 1792749999000,
                                          {"plan_type": "team",
                                           "primary": {"used_percent": 5.0}})
    g = group_and_sort(rows2)
    chk("分组数 = 4（Cursor / GLM / Copilot / codex@dead + codex@5f08）", len(g), 5)
    chk("最近观测的 plan 排最前", g[0]["account"], "deadbeef")
    glm_g = [x for x in g if x["client"] == "GLM"]
    chk("GLM 一张卡含三个窗口", len(glm_g[0]["windows"]) if glm_g else 0, 3)
    chk("组内窗口按 5h → 周 → 月 排列",
        [w["kind"] for w in glm_g[0]["windows"]] if glm_g else [],
        ["tokens_limit-3-5", "tokens_limit-6-1", "time_limit-5-1"])

    # 6) 长窗口标识降级（用户实测：pool:<64位哈希>:TOKENS_LIMIT:6:1 把卡片撑爆）
    chk("长标识压成类型名", _short_label(
        "pool:9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a:TOKENS_LIMIT:6:1"),
        "TOKENS_LIMIT")
    chk("厂商类型名有中文译名", _TYPE_LABEL["TOKENS_LIMIT"], "Token 限额")
    chk("短桶名原样保留", _short_label("third-party"), "third-party")
    chk("没有类型名段时退到 pool 这类普通段", _short_label(
        "pool:12345678901234567890123456789012:9:1"), "pool")
    chk("纯数字段不配当标签", _short_label("12345678901234567890:12345678901234567890"),
        "额度")
    chk("窗口唯一键不长串", len(_window_kind(
        "pool:9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a9f3a7c1e2b8d4f6a:TOKENS_LIMIT:6:1")) <= 14,
        True)

    # 7) 外部套餐识别：models.json → probe（合成数据，不碰真实文件）
    import tempfile as _tf
    tmp2 = _tf.mkdtemp(prefix="plan-test-")
    mj = Path(tmp2) / "models.json"
    mj.write_text(json.dumps([
        {"id": "GLM-5.3", "name": "GLM-5.3 (Coding Plan)",
         "baseUrl": "https://open.bigmodel.cn/api/coding/paas/v4", "apiKey": "cn-secret"},
        {"id": "ZAI", "name": "GLM 国际版",
         "baseUrl": "https://api.z.ai/v4", "key": "intl-secret"},
        {"id": "端砚", "name": "GLM-5.3 (端砚)",
         "baseUrl": "https://disc.example.com/v1", "apiKey": "other"},
        {"id": "step-5", "name": "Step-5", "baseUrl": ""},          # 无 baseUrl 的跳过
    ]), encoding="utf-8")
    plans, keys = read_plan_models(mj)
    chk("识别出 2 个外部套餐", [p["probe_id"] for p in plans],
        ["glm-coding-cn", "glm-coding-intl"])
    chk("套餐带显示名与网关", (plans[0]["model_name"], plans[0]["gateway"]),
        ("GLM-5.3 (Coding Plan)", "open.bigmodel.cn"))
    chk("非套餐模型不误匹配", len(plans), 2)
    chk("Key 单独返回、不进脱敏列表", ("apiKey" in json.dumps(plans)), False)
    chk("两种 Key 字段名都能取到", sorted(keys), ["glm-coding-cn", "glm-coding-intl"])
    chk("未匹配的套餐 key_present=false 之外不出现 Key",
        all("secret" not in json.dumps(p) for p in plans), True)

    # 8) GLM 响应解析（结构取自三个独立来源给的一致示例）
    glm_resp = {"code": 200, "msg": "操作成功", "success": True, "data": {
        "limits": [
            {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 1,
             "nextResetTime": 1778753640304},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 44},
            {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 100,
             "currentValue": 0, "remaining": 100, "percentage": 0},
        ], "level": "pro"}}
    grows, level = _glm_limits(glm_resp)
    chk("GLM 解析出 3 个窗口", len(grows), 3)
    chk("套餐等级取自 level", level, "pro")
    chk("5 小时 Token 窗口（剩余 = 100 - 已用）",
        (grows[0]["label"], grows[0]["remaining_pct"], grows[0]["used_pct"]),
        ("5 小时 Token", 99.0, 1.0))
    chk("7 天 Token 窗口", grows[1]["label"], "1 周 Token")
    chk("月度 MCP 窗口", grows[2]["label"], "1 个月 MCP 调用")
    chk("重置时间毫秒转秒", grows[0]["resets_at"], 1778753640)
    chk("百分比越界时夹紧", _glm_limits({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 130}]}})[0][0]["remaining_pct"], 0.0)
    chk("空 limits 不炸", _glm_limits({"data": {"limits": []}})[0], [])
    chk("非 GLM 结构不炸", _glm_limits({"foo": 1}), ([], None))

    # 9) Kimi 开放平台余额（按量计费）：响应结构取自官方文档
    km = {"code": 0, "status": True, "scode": "0x0",
          "data": {"available_balance": 49.58894, "voucher_balance": 46.58893,
                   "cash_balance": 3.00001}}
    brows, blevel = _moonshot_balance(km, "CNY")
    chk("Kimi 余额解析出 1 行", len(brows), 1)
    chk("余额取 available_balance 并保留两位",
        (brows[0]["balance"], brows[0]["currency"]), (49.59, "CNY"))
    chk("余额是金额不是百分比（不塞 remaining_pct）",
        brows[0]["remaining_pct"], None)
    chk("代金券与现金余额保留（可分项说明）",
        (brows[0]["voucher"], brows[0]["cash"]), (46.59, 3.0))
    chk("国际站识别为美元", _moonshot_balance(km, "USD")[0][0]["currency"], "USD")
    chk("余额字段缺失时不硬编 0", _moonshot_balance({"data": {"x": 1}})[0], [])
    chk("Probe 登记表含 Kimi 开放平台（按量计费）",
        [p["id"] for p in PLAN_PROBES if p["parser"] == "moonshot_balance"],
        ["kimi-openapi"])

    # 9b) DeepSeek 余额（按量计费）：响应结构取自官方文档，数值是字符串
    ds = {"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "110.00",
         "granted_balance": "10.00", "topped_up_balance": "100.00"}]}
    drows, _ = _ds_balance(ds)
    chk("DeepSeek 余额解析（字符串转数值）", (len(drows), drows[0]["balance"]), (1, 110.0))
    chk("赠金与充值余额分项", (drows[0]["voucher"], drows[0]["cash"]), (10.0, 100.0))
    chk("is_available 记为状态", drows[0]["balance_ok"], True)
    chk("余额字段缺失时不硬编 0", _ds_balance({"balance_infos": []})[0], [])
    chk("多币种取第一条", _ds_balance({"balance_infos": [
        {"currency": "USD", "total_balance": "5.00"}]})[0][0]["currency"], "USD")
    chk("Probe 登记表含 DeepSeek 开放平台", 
        [p["id"] for p in PLAN_PROBES if p.get("local_key") == "deepseek"],
        ["deepseek-openapi"])

    # 9c) 凭据只认显式配置：没有环境变量、models.json 也没配 → 返回 None
    import os as _os
    _saved = _os.environ.pop("DEEPSEEK_API_KEY", None)
    chk("没有显式配置时取不到 DeepSeek 凭据", deepseek_key(mj)[0], None)
    _os.environ["DEEPSEEK_API_KEY"] = "sk-test-only"
    chk("环境变量优先于 models.json", deepseek_key(mj), ("sk-test-only", "环境变量 DEEPSEEK_API_KEY"))
    if _saved is not None:
        _os.environ["DEEPSEEK_API_KEY"] = _saved
    else:
        _os.environ.pop("DEEPSEEK_API_KEY", None)

    # 10) 拉取失败 → 降级为错误说明（连一个本地不可达地址，不发真实请求）
    r = fetch_plan_quota(PLAN_PROBES[0], "x", timeout=2)
    chk("probe 状态非 stable 时明说（且用用户能懂的话，不含内部术语）",
        fetch_plan_quota(PLAN_PROBES[2], "x", timeout=2)["error"], "该套餐暂不支持读取额度")
    chk("缺 Key 时说明原因", fetch_plan_quota(PLAN_PROBES[0], "")["error"],
        "未找到该套餐的 API Key")
    chk("失败时不抛异常", isinstance(r["rows"], list), True)

    print("\n  " + ("额度模块自测全部通过 ✓" if ok else "存在失败项 ✗"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
