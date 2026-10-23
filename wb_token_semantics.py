#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy / Agent HUD Token 计量语义 —— 参考实现（golden reference）

本模块是整套统计体系的**唯一口径来源**，不负责渲染、不负责输出文件。
任何前端（Python 报告、本地 Web 服务、浏览器端 JS 移植版）都必须产出与本模块一致的数值；
改动本模块必须通过 test_semantics.py 的回归测试（含 12 天 HUD 基线对账）。

═══════════════════════════════════════════════════════════════════
三条口径铁律（全部由 HUD 完好窗口实证，不是推测）
═══════════════════════════════════════════════════════════════════

【R1】事件粒度 = 任何带 providerData.usage 的 JSON 行
     —— 不是「assistant 消息」。实测约 90% 的用量落在 function_call 行上，
        只数 assistant 消息会漏掉九成。

【R2】inputTokens 已包含 cached_tokens（OpenAI 语义）
     tokens_in  = inputTokens - cached_tokens     （净新增输入）
     cache_read = cached_tokens
     若把 cached 再加一次，总量会虚高约 2.4 倍。

【R3】outputTokens 已包含 reasoning_tokens
     tokens_out = outputTokens
     reasoning 不再单列（否则重复计数；HUD 对 WorkBuddy 亦不单列）

     总量 = inputTokens + outputTokens
     （缓存读取只计一次；即 tokens_in + cache_read + tokens_out）

补充规则
  · 同一会话文件在多个目录名下（symlink / 硬链接镜像）按 realpath 去重
  · 会话正在写入时末行可能不完整 → JSON 解析失败直接跳过，不算错误
  · 时间戳字段为毫秒级 Unix 时间（timestamp），本地时区归日
  · **只扫描 projects/<项目>/<会话>.jsonl 这一层，不进入 <会话>/subagents/**
    —— HUD 会登记子代理文件路径却**不导入**其用量（实测：某子代理 62 个事件的
       timestamp 在账本中命中 0 次）。计入子代理会与 HUD 口径不一致：
      本机实测相差 16 个文件 / 237 次调用 / 1,149 万 token（约 0.4%）。
      若产品需要「真实消耗」口径，应作为显式开关，不能默认打开。

校准记录（2026-10-21，用 HUD 账本事故前的完好数据做标准答案）
  10-08~10-19 共 12 天：8 天总量偏差 0.00%（分毫不差），最大 1.27%
  事件数 9 天完全相等
═══════════════════════════════════════════════════════════════════

用法
    from wb_token_semantics import collect_transcripts, collect_ledger, aggregate
    events = list(collect_transcripts(Path.home() / ".workbuddy" / "projects"))
    stats = aggregate(events)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import shutil
import subprocess

SEMANTICS_VERSION = "1.0.0"

# JS 移植版必须镜像的字段路径（写明在此，防止两边漂移）
TRANSCRIPT_USAGE_PATH = ("providerData", "usage")
TRANSCRIPT_TIME_PATH = ("timestamp",)
TRANSCRIPT_MODEL_PATH = ("providerData", "model")
REQUEST_MODEL_ID_PATH = ("providerData", "requestModelId")
CACHED_TOKENS_PATH = ("inputTokensDetails", 0, "cached_tokens")
REASONING_TOKENS_PATH = ("outputTokensDetails", 0, "reasoning_tokens")

CLIENT_PREFIX = "workbuddy"


# ------------------------------------------------------------------ 单行解析


def _dig(obj, path):
    """按路径取值，任何一层缺失返回 None（不抛异常）。"""
    cur = obj
    for key in path:
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return None
        if cur is None:
            return None
    return cur


def parse_transcript_line(line: str, session_id: Optional[str] = None) -> Optional[dict]:
    """把 transcript 的一行文本解析为一个 TokenEvent；不是用量行则返回 None。

    这是 R1~R3 的落地实现，也是 JS 版必须逐字对齐的函数。
    """
    # 快速过滤：绝大多数行不含 usage，先做子串判断再 parse（性能关键）
    if '"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except Exception:
        return None  # 会话写入中末行可能被截断，属正常情况

    usage = _dig(d, TRANSCRIPT_USAGE_PATH)
    ts = _dig(d, TRANSCRIPT_TIME_PATH)
    if not usage or ts is None:
        return None

    tin_gross = usage.get("inputTokens") or 0
    cached = _dig(usage, CACHED_TOKENS_PATH) or 0
    tout = usage.get("outputTokens") or 0

    # R2：inputTokens 含 cached → 净新增输入
    tin = tin_gross - cached
    if tin < 0:  # 数据异常保护：个别供应商把 cached 报成大于 input
        tin = 0

    model = _dig(d, TRANSCRIPT_MODEL_PATH) or "unknown"
    # 关键：agent 用 requestModelId 而不是 model。
    # 自定义模型的 requestModelId 带 custom-local: 前缀，而 model 字段只是个名字——
    # 用 model 会把「内置同名模型」与「自定义同名模型」混成一行（step-5-preview 就踩过）。
    rid = _dig(d, REQUEST_MODEL_ID_PATH) or model
    rid = str(rid)
    channel = "byok" if rid.lower().startswith("custom-local:") else "builtin"

    return {
        "ts_ms": int(ts),
        "session": session_id,
        "client": CLIENT_PREFIX,
        "model": model,
        "channel": channel,
        "agent": f"{CLIENT_PREFIX}-model:{rid}",
        # R2 / R3 落地
        "tin": tin,
        "tout": tout,          # 已含 reasoning
        "cread": cached,
        "cwrite": 0,
        "reasoning": 0,        # 不单列，避免与 tout 重复
        # 保留原始值，便于对账与调试（不参与总量计算）
        "_raw_in": tin_gross,
        "_raw_out": tout,
    }


# ------------------------------------------------------------------ 会话名称
# 侧边栏显示的会话名来自 transcript 里的 ai-title 行（字段 aiTitle）。
# 实测覆盖率 82%，其余用「首条用户消息」兜底，再不行才退回会话 UUID。

_SYS_REMINDER_RE = re.compile(r"<system-reminder[\s\S]*?</system-reminder>", re.I)
_TAG_RE = re.compile(r"<[^>]{1,200}>")


def parse_title_line(line: str) -> Optional[str]:
    """从 ai-title 行取会话名；不是标题行返回 None。"""
    if '"ai-title"' not in line:
        return None
    try:
        d = json.loads(line)
    except Exception:
        return None
    if d.get("type") != "ai-title":
        return None
    t = (d.get("aiTitle") or "").strip()
    return t or None


def parse_user_preview(line: str, limit: int = 48) -> Optional[str]:
    """从首条用户消息提取可读摘要（剥掉 system-reminder 与标签）；失败返回 None。"""
    if '"user"' not in line:
        return None
    try:
        d = json.loads(line)
    except Exception:
        return None
    if d.get("type") != "message" or d.get("role") != "user":
        return None
    content = d.get("content")
    texts = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in ("input_text", "text"):
                texts.append(c.get("text") or "")
    elif isinstance(content, str):
        texts.append(content)
    if not texts:
        return None
    s = "".join(texts)
    s = _SYS_REMINDER_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 2:
        return None
    return s[:limit] + ("…" if len(s) > limit else "")


def event_total(ev: dict) -> int:
    """单个事件的 token 总量。

    必须与 aggregate() 里 _finish() 的公式逐项一致，否则「按日×模型」维度会
    与总计对不上（历史 bug：漏了账本事件的 cache_write 与 reasoning，少算 165,908）。
    transcripts 来源的 cwrite / reasoning 恒为 0，故两种数据源都能正确覆盖。
    """
    return ev["tin"] + ev["cread"] + ev["tout"] + ev["cwrite"] + ev["reasoning"]


UNKNOWN_SESSION = "(unknown)"


def session_key(ev: dict) -> str:
    """会话标识；缺失时归入 UNKNOWN_SESSION，不允许事件被静默丢弃。

    历史 bug：用 `if ev.get('session')` 直接跳过无 id 的事件，导致
    by_session 合计比总计少 32,144,379（账本里部分 contribution key 无会话段）。
    """
    return ev.get("session") or UNKNOWN_SESSION


# ------------------------------------------------------------------ 数据源 A：transcripts


def collect_transcripts(
    projects_dir: Path,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    include_subagents: bool = False,
    session_meta: Optional[dict] = None,
) -> Iterator[dict]:
    """遍历 projects 各项目目录下的 .jsonl，产出 TokenEvent。

    按 realpath 去重 —— 工作目录改名后新旧项目目录可能指向同一实体，
    不去重会把同一份会话算两遍（实测会让「其他-个人与临时」翻倍）。

    session_meta
      传入 dict 时，在扫描过程中顺带收集会话名称（ai-title 优先，首条用户消息兜底），
      形如 {session_id: {"title": str|None, "preview": str|None}}。
      这是纯附加元数据，不影响任何 token 统计口径。

    include_subagents
      False（默认）：只扫 projects/<项目>/<会话>.jsonl 一层 —— 与 HUD 口径一致。
      True         ：额外扫 <项目>/<会话>/subagents/agent-*.jsonl。这些用量 HUD
                     登记文件但不导入（实测 62 个事件时间戳在账本命中 0 次），
                     打开后会比 HUD 高（本机约 +237 次 / +1,149 万 token）。
                     这是**显式选择**，不是默认行为。
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return
    seen_real: set[str] = set()

    # (文件路径, 会话 UUID, 是否子代理, 项目目录名)
    targets: list[tuple[Path, str, bool, str]] = []
    for f in sorted(projects_dir.glob("*/*.jsonl")):
        targets.append((f, f.stem, False, f.parent.name))
    if include_subagents:
        for f in sorted(projects_dir.glob("*/*/subagents/*.jsonl")):
            # <项目>/<会话UUID>/subagents/agent-x.jsonl → 归属父会话
            targets.append((f, f.parent.parent.name, True, f.parent.parent.parent.name))

    for f, session_id, is_sub, project in targets:
        try:
            real = os.path.realpath(f)
        except OSError:
            continue
        if real in seen_real:
            continue
        seen_real.add(real)
        try:
            with open(real, errors="replace") as fh:
                for line in fh:
                    if session_meta is not None:
                        rec = session_meta.get(session_id)
                        if rec is None:
                            rec = session_meta[session_id] = {"title": None, "preview": None}
                        if not rec["title"]:
                            t = parse_title_line(line)
                            if t:
                                rec["title"] = t
                        if not rec["title"] and not rec["preview"]:
                            pv = parse_user_preview(line)
                            if pv:
                                rec["preview"] = pv
                    ev = parse_transcript_line(line, session_id)
                    if ev is None:
                        continue
                    if since_ms is not None and ev["ts_ms"] < since_ms:
                        continue
                    if until_ms is not None and ev["ts_ms"] > until_ms:
                        continue
                    ev["project"] = project
                    ev["subagent"] = is_sub
                    yield ev
        except OSError:
            continue


# ------------------------------------------------------------------ 数据源 B：HUD 账本


def collect_ledger(
    db_path: Path,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    exclude_workbuddy: bool = False,
) -> Iterator[dict]:
    """只读 HUD 账本（WAL 模式可并发读），产出 TokenEvent。

    账本里 WorkBuddy 部分的语义由 HUD 自己算，本模块不再重算；
    但字段名统一映射为 TokenEvent，让下游无需区分数据源。

    exclude_workbuddy=True 时跳过 workbuddy 事件 —— 「auto」混合模式用它，
    避免与 transcripts 通道重复计数。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT e.timestamp_ms, e.tokens_in, e.tokens_out, e.cache_read,
                   e.cache_write, e.reasoning, a.name AS agent, c.key AS contrib_key
            FROM usage_event e
            JOIN agent a        ON a.id = e.agent
            JOIN contribution c ON c.id = e.contribution_id AND c.counted = 1
            WHERE 1 = 1
        """
        params: list = []
        if since_ms is not None:
            sql += " AND e.timestamp_ms >= ?"
            params.append(int(since_ms))
        if until_ms is not None:
            sql += " AND e.timestamp_ms <= ?"
            params.append(int(until_ms))
        sql += " ORDER BY e.timestamp_ms"
        for r in con.execute(sql, params):
            agent = r["agent"]
            client = agent.partition("-model:")[0] if "-model:" in agent else "other"
            model = agent.partition("-model:")[2] if "-model:" in agent else agent
            if exclude_workbuddy and client == CLIENT_PREFIX:
                continue
            # 账本里 workbuddy 的 session 从 contribution key 取（形如 workbuddy:<uuid>）
            session = None
            if r["contrib_key"] and ":" in r["contrib_key"]:
                session = r["contrib_key"].partition(":")[2]
            yield {
                "ts_ms": int(r["timestamp_ms"]),
                "session": session,
                "client": client,
                "model": model,
                "agent": agent,
                "tin": r["tokens_in"],
                "tout": r["tokens_out"],
                "cread": r["cache_read"],
                "cwrite": r["cache_write"],
                "reasoning": r["reasoning"],
                "project": "(HUD 账本)",   # 账本事件无项目目录概念
                "channel": "ledger",       # 账本侧无法区分内置/自带 Key
                "subagent": False,
                "_raw_in": r["tokens_in"] + r["cache_read"],
                "_raw_out": r["tokens_out"],
            }
    finally:
        con.close()


# ------------------------------------------------------------------ 混合数据源


def collect_auto(
    projects_dir: Path,
    db_path: Path,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    include_subagents: bool = False,
    session_meta: Optional[dict] = None,
) -> Iterator[dict]:
    """auto 模式：WorkBuddy 走 transcripts（完整且免疫路径改名），
    其余客户端（Codex / Claude Code / DeepSeek）走 HUD 账本。
    """
    yield from collect_transcripts(projects_dir, since_ms, until_ms, include_subagents, session_meta)
    yield from collect_ledger(db_path, since_ms, until_ms, exclude_workbuddy=True)


# ------------------------------------------------------------------ 数据源 C/D/E：直读各客户端的会话日志
#
# 为什么直读、而不是只靠 HUD 账本：HUD 本身只是个读取器，读的就是下面这些文件；
# 直读才能跨平台（Windows 上同样有 %USERPROFILE%\.codex / .claude / .dsh），
# 也能拿到 HUD 没导入的尾部数据。
#
# 三家口径已逐文件与 HUD 账本对账过（2026-10-23，本机）：
#   codex   Δ +0.0%   6 个有数据的文件全部逐字节相等
#   claude  Δ 0       316,986 = 316,986
#   dsh     Δ +0.8%   我方 41 次 > 账本 40 次，差额 = 账本漏记的那一条
# 三家的 R1~R3 与 WorkBuddy **不同**，谁把它们混为一谈就会错 2 倍以上，故逐条注释。

CODEX_CLIENT = "codex"
CLAUDE_CLIENT = "claude"
DSH_CLIENT = "deepseek"          # 账本里 DeepSeek Harness 的 source 名就是 deepseek

_ROLLOUT_UUID_RE = re.compile(
    r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", re.I)


def _iso_to_ms(v) -> Optional[int]:
    """ISO8601 / 毫秒 / 秒 → 毫秒。三家客户端的 timestamp 写法各不相同。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 1e11 else int(v * 1000)
    t = str(v).strip()
    if not t:
        return None
    if t.isdigit():
        n = int(t)
        return n if n > 1e11 else n * 1000
    try:
        return int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _find_zstd() -> Optional[str]:
    """找 zstd 可执行文件。

    不能只靠 shutil.which：macOS 的 LaunchAgent 只有最小 PATH，
    Homebrew 的 /opt/homebrew/bin 不在其中（实测会误报"缺 zstd"）。
    Windows 用户则通常根本没有这个命令 —— 所以发布版应内置解码能力，
    这里的多路径搜索只是兜底。
    """
    env = os.environ.get("ZSTD_BIN")
    if env and Path(env).is_file():
        return env
    hit = shutil.which("zstd")
    if hit:
        return hit
    cands = ["/opt/homebrew/bin/zstd", "/usr/local/bin/zstd", "/usr/bin/zstd",
             "/opt/local/bin/zstd"]
    for c in cands:
        if Path(c).is_file():
            return c
    # 与可执行文件同目录（打包后把 zstd 放在旁边即可）
    try:
        base = Path(sys.executable).resolve().parent
        for name in ("zstd", "zstd.exe"):
            p = base / name
            if p.is_file():
                return str(p)
    except Exception:
        pass
    return None


def _zstd_text(path: Path) -> tuple[Optional[str], str]:
    """dsh 的 .zstd 是**多帧**拼接，不是单帧 gzip 之类。
    依次尝试：Python 3.14 标准库 → 第三方 zstandard → 本机 zstd CLI → 同目录 zstd。"""
    try:
        from compression import zstd as _z          # Python 3.14+
        return _z.decompress(path.read_bytes()).decode("utf-8", "replace"), "py314"
    except Exception:
        pass
    try:
        import zstandard                            # type: ignore
        with open(path, "rb") as fh:
            data = zstandard.ZstdDecompressor().stream_reader(fh).read()
        return data.decode("utf-8", "replace"), "zstandard"
    except Exception:
        pass
    exe = _find_zstd()
    if exe:
        try:
            r = subprocess.run([exe, "-dc", str(path)], capture_output=True, timeout=180)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace"), "cli"
        except Exception:
            pass
    return None, "none"


def _ev(ts_ms: int, session: str, client: str, model: str,
        tin: int, tout: int, cread: int = 0, cwrite: int = 0, reasoning: int = 0) -> dict:
    """构造一个 TokenEvent，字段与 parse_transcript_line 完全一致。"""
    return {
        "ts_ms": int(ts_ms), "session": session, "client": client,
        "model": model, "channel": "builtin",
        "agent": f"{client}-model:{model}",
        "tin": max(0, int(tin)), "tout": int(tout), "cread": int(cread),
        "cwrite": int(cwrite), "reasoning": int(reasoning),
        "_raw_in": int(tin) + int(cread), "_raw_out": int(tout),
    }


_CLIENT_LABEL = {"codex": "Codex", "claude": "Claude Code", "deepseek": "DSH"}


def _note_title(session_meta: Optional[dict], sid: str, client: str, proj: str) -> None:
    """给非 WorkBuddy 的会话补一个可读名字。

    这三家没有 ai-title 之类的标题行，若不补，会话表里就只剩一串 UUID，
    对使用者毫无意义。用「客户端 · 项目名」作标题，不碰已有标题。
    """
    if session_meta is None or not sid:
        return
    rec = session_meta.get(sid) or {}
    if rec.get("title"):
        return
    label = _CLIENT_LABEL.get(client, client)
    session_meta[sid] = {"title": f"{label} · {proj}", "preview": rec.get("preview")}


def collect_codex(
    root: Path,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    session_meta: Optional[dict] = None,
    stats: Optional[dict] = None,
    quota_out: Optional[list] = None,
) -> Iterator[dict]:
    """Codex CLI 的 rollout 日志。

    口径（与 HUD 逐字节相等，已验）：
      R1' 事件 = `event_msg` 行里 payload.type == "token_count" 的
          `info.last_token_usage`；若整个文件没有 token_count，退回
          `turn.completed` 的 `usage`（老版本只有这个）。
      R2' Codex 的 input_tokens **含** cached_input_tokens → 必须扣；
          与 Claude 相反，照搬 Claude 的算法会把输入算成两倍。
      R3' output_tokens 已含 reasoning_output_tokens → 不再单列。
    """
    root = Path(root)
    if not root.is_dir():
        return
    files = sorted(root.glob("**/rollout-*.jsonl"))
    if stats is not None:
        stats.setdefault(CODEX_CLIENT, {})["files"] = len(files)
    for path in files:
        m = _ROLLOUT_UUID_RE.search(path.name)
        sid = m.group(1) if m else path.stem
        model = "unknown"
        try:
            fallback_ts = int(path.stat().st_mtime * 1000)
        except OSError:
            fallback_ts = 0
        last_ts = None
        cwd = None
        is_sub = False
        hits = []
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    # 快速预过滤（性能）：只放行可能是元信息或用量行的行。
                    # 注意不能写 '"usage"' —— Codex 的字段叫 last_token_usage，
                    # 带引号的 "usage" 子串并不存在，那样会把全部用量行滤掉。
                    if ("token_count" not in line and "turn.completed" not in line
                            and '"model"' not in line and '"cwd"' not in line
                            and "parent_thread_id" not in line):
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = _iso_to_ms(d.get("timestamp"))
                    if ts is not None:
                        last_ts = ts
                    t = d.get("type")
                    p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                    if t == "session_meta":
                        sid = p.get("id") or p.get("session_id") or sid
                        cwd = p.get("cwd") or cwd
                        is_sub = bool(p.get("parent_thread_id"))
                    elif t == "turn_context":
                        model = p.get("model") or p.get("model_name") or model
                    elif t == "event_msg" and p.get("type") == "token_count":
                        u = (p.get("info") or {}).get("last_token_usage") or {}
                        if u:
                            hits.append(("a", u))
                        # 顺手把额度窗口带出来：同一次扫描，不再读一遍 75MB 的 rollout
                        rl = p.get("rate_limits")
                        if quota_out is not None and isinstance(rl, dict):
                            quota_out.append(((ts or fallback_ts) if ts else fallback_ts,
                                              "default", rl))
                    elif t == "turn.completed":
                        u = d.get("usage") or {}
                        if u:
                            hits.append(("b", u))
        except OSError:
            continue

        # 两种事件同时存在时，只用每次调用一条的 token_count（否则整轮再算一遍）
        kind = "a" if any(k == "a" for k, _ in hits) else "b"
        used = 0
        unknown = 0
        proj_name = Path(cwd).name if cwd else path.parent.name
        _note_title(session_meta, sid, CODEX_CLIENT, proj_name)
        for k, u in hits:
            if k != kind:
                continue
            i = int(u.get("input_tokens") or 0)
            c = int(u.get("cached_input_tokens") or 0)
            ev = _ev(last_ts or fallback_ts, sid, CODEX_CLIENT, model,
                     tin=i - c, tout=int(u.get("output_tokens") or 0), cread=c)
            ev["project"] = "codex/" + (Path(cwd).name if cwd else path.parent.name)
            ev["subagent"] = is_sub
            ev["_raw_in"] = i
            if since_ms is not None and ev["ts_ms"] < since_ms:
                continue
            if until_ms is not None and ev["ts_ms"] > until_ms:
                continue
            used += 1
            if model == "unknown":
                unknown += 1
            yield ev
        if stats is not None:
            st = stats.setdefault(CODEX_CLIENT, {})
            st["events"] = st.get("events", 0) + used
            # 只有"真的有用量事件却认不出模型"才提示；没事件的文件不算
            if unknown:
                st["note"] = f"{unknown} 次调用未能识别模型名"


def collect_claude(
    root: Path,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    include_subagents: bool = False,
    session_meta: Optional[dict] = None,
    stats: Optional[dict] = None,
) -> Iterator[dict]:
    """Claude Code 的 project 转录。

    口径（已精确对上 HUD：316,986 = 316,986）：
      R1' 事件 = type == "assistant" 且 message.usage 非空。
      R2' Claude 的 input_tokens **不含**缓存；三个缓存字段各自独立
          → tin = input_tokens，cread = cache_read_input_tokens，
            cwrite = cache_creation_input_tokens。
      R3' output_tokens 已含 thinking（output_tokens_details.thinking_tokens 是其明细）
          → 不再单列。
      去重（关键）：同一个 message.id 会写多行（每个 content block 一行），
          而且**首行常是 0（中间态）、真实值在后面**。
          → 同 id 取"用量最大的一行"，不能取首行，否则整文件归零。
    """
    root = Path(root)
    if not root.is_dir():
        return
    files = []
    for p in sorted(root.glob("**/*.jsonl")):
        if not include_subagents and "subagents" in p.parts:
            continue
        files.append(p)
    if stats is not None:
        stats.setdefault(CLAUDE_CLIENT, {})["files"] = len(files)

    for path in files:
        sid = path.stem
        cwd = None
        best: dict[str, tuple] = {}
        multi = 0
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(d, dict):
                        continue
                    sid = d.get("sessionId") or sid
                    cwd = d.get("cwd") or cwd
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                    u = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                    if not u:
                        continue
                    key = msg.get("id") or d.get("uuid") or f"{path.stem}:{len(best)}"
                    tin = int(u.get("input_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    cw = int(u.get("cache_creation_input_tokens") or 0)
                    out = int(u.get("output_tokens") or 0)
                    rec = (tin + cr + cw + out, tin, cr, cw, out,
                           msg.get("model") or "unknown", _iso_to_ms(d.get("timestamp")))
                    if key in best:
                        if rec[0] > best[key][0]:
                            best[key] = rec
                        elif rec[0] > 0 and best[key][0] > 0 and rec[1:] != best[key][1:]:
                            multi += 1
                    else:
                        best[key] = rec
        except OSError:
            continue

        used = 0
        proj_name = Path(cwd).name if cwd else path.parent.name
        _note_title(session_meta, sid, CLAUDE_CLIENT, proj_name)
        for tot, tin, cr, cw, out, model, ts in best.values():
            if tot <= 0:
                continue        # 只有中间态 0 记录，没拿到真实用量
            ev = _ev(ts or 0, sid, CLAUDE_CLIENT, model, tin=tin, tout=out,
                     cread=cr, cwrite=cw)
            ev["project"] = "claude/" + (Path(cwd).name if cwd else path.parent.name)
            ev["subagent"] = "subagents" in path.parts
            if since_ms is not None and ev["ts_ms"] < since_ms:
                continue
            if until_ms is not None and ev["ts_ms"] > until_ms:
                continue
            used += 1
            yield ev
        if stats is not None:
            st = stats.setdefault(CLAUDE_CLIENT, {})
            st["events"] = st.get("events", 0) + used
            if multi:
                st["note"] = f"{multi} 个 message.id 出现多组非零用量（可能低估）"


def collect_dsh(
    root: Path,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    session_meta: Optional[dict] = None,
    stats: Optional[dict] = None,
) -> Iterator[dict]:
    """DeepSeek Harness（dsh）的会话日志（zstd 多帧 JSONL）。

    口径（Δ +0.8%，差额来自账本漏记的一条）：
      R1' 事件 = `assistant/message` 里的 `data.usage`（camelCase）。
      R2' `inputTokens` 是**净新增**（不含缓存）；缓存单独是 `cacheReadTokens`
          （注意键名：不是 cachedInputTokens）。
      R3' `totalTokens = inputTokens + outputTokens` —— reasoningTokens **已含在
          outputTokens 里**，再加就重复。
      防重复：同一份用量还会在 `data.stream[].chunk.usage` 再写一次
          → 每个事件只取第一个命中的 usage。
    """
    root = Path(root)
    if not root.is_dir():
        return
    files = sorted(p for p in root.glob("**/session*.jsonl*")
                   if p.suffix in (".zstd", ".jsonl") or p.name.endswith(".jsonl.zstd"))
    if stats is not None:
        stats.setdefault(DSH_CLIENT, {})["files"] = len(files)
    backend_seen = set()

    for path in files:
        if path.name.endswith(".zstd"):
            text, backend = _zstd_text(path)
            backend_seen.add(backend)
        else:
            try:
                text, backend = path.read_text(errors="replace"), "plain"
            except OSError:
                continue
            backend_seen.add(backend)
        if text is None:
            if stats is not None:
                stats.setdefault(DSH_CLIENT, {})["note"] = "无法解压（缺 zstd）"
            continue

        sid = path.parent.name or path.stem
        cwd = None
        model = "unknown"
        depth = 0
        created = None
        used = 0
        proj_name = ""
        for line in text.splitlines():
            line = line.strip()
            if not line or '"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "session":
                sid = d.get("id") or sid
                cwd = d.get("cwd") or cwd
                depth = int(d.get("delegationDepth") or 0)
                created = _iso_to_ms(d.get("createdAt"))
                continue
            data = d.get("data") if isinstance(d.get("data"), dict) else {}
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            src = msg.get("source") if isinstance(msg.get("source"), dict) else {}
            if src.get("model"):
                model = src["model"]
            else:
                hdr = data.get("header") if isinstance(data.get("header"), dict) else {}
                cfg = hdr.get("config") if isinstance(hdr.get("config"), dict) else {}
                if cfg.get("model"):
                    model = cfg["model"]
            if cwd and not proj_name:
                proj_name = Path(cwd).name
                _note_title(session_meta, sid, DSH_CLIENT, proj_name)
            u = data.get("usage")
            if not isinstance(u, dict) or not u:
                continue
            tin = int(u.get("inputTokens") or u.get("input") or 0)
            cr = int(u.get("cacheReadTokens") or u.get("cachedInputTokens")
                     or u.get("cachedInput") or 0)
            cw = int(u.get("cacheWriteTokens") or u.get("cacheWrite") or 0)
            out = int(u.get("outputTokens") or u.get("output") or 0)
            ts = _iso_to_ms(d.get("time")) or created or 0
            ev = _ev(ts, sid, DSH_CLIENT, model, tin=tin, tout=out, cread=cr, cwrite=cw)
            ev["project"] = "dsh/" + (Path(cwd).name if cwd else path.parent.name)
            ev["subagent"] = depth > 0
            if since_ms is not None and ev["ts_ms"] < since_ms:
                continue
            if until_ms is not None and ev["ts_ms"] > until_ms:
                continue
            used += 1
            yield ev
        if stats is not None:
            st = stats.setdefault(DSH_CLIENT, {})
            st["events"] = st.get("events", 0) + used
            st["backend"] = "/".join(sorted(backend_seen))
        if not proj_name:      # header 里没有 cwd 时的兜底
            _note_title(session_meta, sid, DSH_CLIENT, path.parent.name)


def collect_all(
    roots: dict,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    include_subagents: bool = False,
    session_meta: Optional[dict] = None,
    stats: Optional[dict] = None,
    use_ledger: bool = False,
    quota_out: Optional[list] = None,
) -> Iterator[dict]:
    """四源合并：WorkBuddy transcripts + Codex + Claude Code + DeepSeek Harness。

    roots 形如 {"workbuddy": Path, "codex": Path, "claude": Path, "dsh": Path,
               "ledger": Path(可选)}
    缺哪个目录就自动跳过哪个（用户没装那家客户端），并在 stats 里留痕。

    use_ledger=True 时额外并进 HUD 账本里**非 WorkBuddy** 的部分。
    默认关闭：账本读的就是上面这些文件的二手结果，同时用会重复计数。
    只有「客户端日志已删除、但 HUD 还留着历史」的场景才需要它。
    """
    if session_meta is None:
        session_meta = {}
    wb = roots.get("workbuddy")
    if wb and Path(wb).is_dir():
        # collect_transcripts 不接收 stats，这里自己数（否则界面上会把
        # 「最大的一个来源」误报成"未找到"，比不报更糟）
        if stats is not None:
            stats.setdefault("workbuddy", {})["files"] = sum(1 for _ in Path(wb).glob("*/*.jsonl"))
        n = 0
        for ev in collect_transcripts(wb, since_ms, until_ms, include_subagents, session_meta):
            n += 1
            yield ev
        if stats is not None:
            stats["workbuddy"]["events"] = n
    if roots.get("codex"):
        yield from collect_codex(roots["codex"], since_ms, until_ms, session_meta, stats,
                                 quota_out=quota_out)
    if roots.get("claude"):
        yield from collect_claude(roots["claude"], since_ms, until_ms,
                                  include_subagents, session_meta, stats)
    if roots.get("dsh"):
        yield from collect_dsh(roots["dsh"], since_ms, until_ms, session_meta, stats)
    if use_ledger and roots.get("ledger"):
        yield from collect_ledger(roots["ledger"], since_ms, until_ms, exclude_workbuddy=True)


# ------------------------------------------------------------------ 客户端适配登记表
#
# 目的：让"支持一个新客户端"变成查表 + 按表操作，而不是每次重新摸索。
# 新增一家的完整流程见交接文档「如何适配一个新客户端」：
#   ① 在下方加一条描述（路径/环境变量/解析函数/额度与积分能力/验证状态）
#   ② 用 probe-keys.py 探字段，用 recon-tokens.py 与 HUD 账本逐文件对账
#   ③ 写 collect_<client>()，字段与 parse_transcript_line 完全一致
#   ④ 在 test_sources.py 补合成夹具用例（不读真实会话）
#   ⑤ 把 client 加进 collect_all 与服务端的 _SRC_DEFAULT
#
# status: stable=已对账 / probe=已探明未对账 / idea=仅有公开资料，未实现
SOURCE_REGISTRY = {
    "workbuddy": {
        "label": "WorkBuddy", "env": [], "parser": "collect_transcripts",
        "paths": ["~/.workbuddy/projects/<项目>/<会话>.jsonl"],
        "quota": None, "credits": "local(每会话 used/size)",
        "status": "stable", "note": "81/82 天金标准；账户剩余积分不在本地",
    },
    "codex": {
        "label": "Codex CLI", "env": ["CODEX_HOME"],
        "parser": "collect_codex",
        "paths": ["~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl"],
        "quota": "rate_limits(5h/7d + 重置时间)", "credits": None,
        "status": "stable", "note": "Δ +0.0% 与 HUD 账本逐字节相等",
    },
    "claude": {
        "label": "Claude Code", "env": ["CLAUDE_CONFIG_DIR"],
        "parser": "collect_claude",
        "paths": ["~/.claude/projects/<slug>/<uuid>.jsonl"],
        "quota": None, "credits": None,
        "status": "stable", "note": "Δ 0；同 message.id 多行须取用量最大行；转录默认 30 天清理",
    },
    "dsh": {
        "label": "DeepSeek Harness", "env": ["DSH_HOME"],
        "parser": "collect_dsh",
        "paths": ["~/.dsh/sessions/<slug>/session-<id>/session.vN.jsonl[.zstd]"],
        "quota": None, "credits": "online(需凭据，HUD 同样读不到)",
        "status": "stable", "note": "Δ +0.8%；zstd 多帧；reasoning 已含在 output",
    },
    "cursor": {
        "label": "Cursor", "env": [], "parser": None,
        "paths": ["（仅经 HUD 账本，仅 macOS）"],
        "quota": "hud-ledger", "credits": None,
        "status": "probe", "note": "用量本身不直读，只取其额度样本",
    },
}


def sources_info() -> list[dict]:
    """适配登记表 + 本机实际探测到的目录，供界面/文档说明"支持什么、缺什么"。"""
    import os
    from pathlib import Path as _P
    home = _P.home()
    guess = {
        "workbuddy": home / ".workbuddy" / "projects",
        "codex": home / ".codex" / "sessions",
        "claude": home / ".claude" / "projects",
        "dsh": home / ".dsh" / "sessions",
    }
    out = []
    for name, spec in SOURCE_REGISTRY.items():
        p = guess.get(name)
        env_hit = None
        for e in spec.get("env") or []:
            if os.environ.get(e):
                env_hit = e
                break
        out.append({
            "name": name, "label": spec["label"], "status": spec["status"],
            "parser": spec["parser"], "paths": spec["paths"],
            "env": spec.get("env") or [], "env_active": env_hit,
            "quota": spec["quota"], "credits": spec["credits"],
            "note": spec["note"],
            "present": bool(p and p.is_dir()),
        })
    return out


# ------------------------------------------------------------------ 聚合


def _bucket() -> dict:
    return {"events": 0, "tin": 0, "tout": 0, "cread": 0, "cwrite": 0, "reasoning": 0}


def _add(b: dict, ev: dict) -> None:
    b["events"] += 1
    b["tin"] += ev["tin"]
    b["tout"] += ev["tout"]
    b["cread"] += ev["cread"]
    b["cwrite"] += ev["cwrite"]
    b["reasoning"] += ev["reasoning"]


def _finish(b: dict) -> dict:
    b["total"] = b["tin"] + b["cread"] + b["tout"] + b["cwrite"] + b["reasoning"]
    return b


def aggregate(events: Iterable[dict]) -> dict:
    """把事件流聚合成各种维度。所有维度共用同一批事件，总量必然自洽。

    返回结构（可直接 JSON 序列化给前端）：
      events         : 事件总数
      totals         : {events,tin,tout,cread,cwrite,reasoning,total}
      by_day         : {"YYYY-MM-DD": bucket}
      by_model       : {"workbuddy-model:xxx": bucket}
      by_day_model   : {"YYYY-MM-DD|agent": total}      （热力图用，压成扁平结构省体积）
      by_hour        : {0..23: bucket}                  （小时级分布）
      by_session     : {"session_id": bucket}           （会话级下钻）
      range          : [最早日期, 最晚日期]
    """
    totals = _bucket()
    by_day: dict[str, dict] = defaultdict(_bucket)
    by_model: dict[str, dict] = defaultdict(_bucket)
    by_day_model: dict[str, int] = defaultdict(int)
    by_hour: dict[int, dict] = defaultdict(_bucket)
    by_session: dict[str, dict] = defaultdict(_bucket)

    n = 0
    ts_min = ts_max = None
    for ev in events:
        n += 1
        ts = ev["ts_ms"]
        dt = datetime.fromtimestamp(ts / 1000)
        day = dt.strftime("%Y-%m-%d")
        agent = ev["agent"]

        if ts_min is None or ts < ts_min:
            ts_min = ts
        if ts_max is None or ts > ts_max:
            ts_max = ts

        _add(totals, ev)
        _add(by_day[day], ev)
        _add(by_model[agent], ev)
        _add(by_hour[dt.hour], ev)
        by_day_model[f"{day}|{agent}"] += event_total(ev)
        _add(by_session[session_key(ev)], ev)

    for coll in (by_day, by_model, by_hour, by_session):
        for k in list(coll):
            coll[k] = _finish(coll[k])
    _finish(totals)

    rng = None
    if ts_min is not None:
        rng = [
            datetime.fromtimestamp(ts_min / 1000).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(ts_max / 1000).strftime("%Y-%m-%d"),
        ]

    return {
        "events": n,
        "totals": totals,
        "by_day": dict(by_day),
        "by_model": dict(by_model),
        "by_day_model": dict(by_day_model),
        "by_hour": dict(by_hour),
        "by_session": dict(by_session),
        "range": rng,
        "semantics_version": SEMANTICS_VERSION,
    }


# ------------------------------------------------------------------ 自检


def selftest_rules() -> list[tuple[str, bool, str]]:
    """对三条铁律做合成数据单元测试（不依赖真实数据，可在任何机器跑）。"""
    out = []

    # R1：function_call 行的 usage 必须被计入（不能只看 assistant 消息）
    line_fc = json.dumps({
        "type": "function_call",
        "timestamp": 1760000000000,
        "providerData": {"model": "m", "usage": {"inputTokens": 100, "outputTokens": 50}},
    })
    ev = parse_transcript_line(line_fc)
    out.append(("R1 function_call 行被计入", ev is not None and ev["ts_ms"] == 1760000000000, ""))

    # R2：inputTokens 含 cached → tin 必须减掉 cached
    line_r2 = json.dumps({
        "type": "function_call",
        "timestamp": 1760000000000,
        "providerData": {
            "model": "m",
            "usage": {"inputTokens": 1000, "outputTokens": 10,
                      "inputTokensDetails": [{"cached_tokens": 800}]},
        },
    })
    ev = parse_transcript_line(line_r2)
    out.append(("R2 tin = input - cached", ev["tin"] == 200 and ev["cread"] == 800,
                f"tin={ev['tin']} cread={ev['cread']}"))
    out.append(("R2 总量不双计缓存", event_total(ev) == 1010, f"total={event_total(ev)}"))

    # R3：outputTokens 含 reasoning → reasoning 不得再单加
    line_r3 = json.dumps({
        "type": "function_call",
        "timestamp": 1760000000000,
        "providerData": {
            "model": "m",
            "usage": {"inputTokens": 10, "outputTokens": 500,
                      "outputTokensDetails": [{"reasoning_tokens": 400}]},
        },
    })
    ev = parse_transcript_line(line_r3)
    out.append(("R3 reasoning 不重复计", event_total(ev) == 510 and ev["reasoning"] == 0,
                f"total={event_total(ev)}"))

    # 无 usage 行必须被忽略
    line_plain = json.dumps({"type": "message", "timestamp": 1760000000000, "role": "user"})
    out.append(("非用量行被忽略", parse_transcript_line(line_plain) is None, ""))

    # 截断行（会话写入中）必须被忽略而非抛异常
    out.append(("截断行安全跳过", parse_transcript_line('{"usage": {broken') is None, ""))

    # 缺失 timestamp 必须被忽略
    line_nots = json.dumps({"providerData": {"usage": {"inputTokens": 5}}})
    out.append(("缺 timestamp 被忽略", parse_transcript_line(line_nots) is None, ""))

    return out


if __name__ == "__main__":
    ok = True
    print(f"wb_token_semantics v{SEMANTICS_VERSION} —— 口径自检\n")
    for name, passed, detail in selftest_rules():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ""))
        ok &= passed
    print(f"\n{'全部通过' if ok else '存在失败项'}")
    raise SystemExit(0 if ok else 1)
