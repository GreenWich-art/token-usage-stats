#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_credits.py — WorkBuddy 积分（本地部分）

数据来源（2026-10-23 实测校正）
  ~/.workbuddy/workbuddy.db 的 session_usage 表：
      session_id  text
      used        integer   **该会话最后一次调用的输入 token 数**（不是积分）
      size        integer   **该会话所用模型的上下文窗口**（1,000,000 / 300,000 /
                            168,000 / 262,144 / 1,048,576 …，随模型而变）
      updated_at  integer   毫秒时间戳
      credit_json text      可选，{"<消息UUID>": 积分}，**逐次调用的真实积分消耗**

  2026-10-23 校正依据（此前把 used/size 误当成「已用积分 / 积分上限」）：
    · size 的取值集合与模型上下文窗口一一对应，且同一账户下不唯一 →
      不可能是账户积分额度；
    · used 与 transcript 里最后一次调用的 inputTokens **逐条完全相等**
      （例：500,876 / 475,311 / 461,273 三例均分毫不差）；
    · used 合计 2,900 万，而 WorkBuddy 设置页显示的积分不到 5,000；
    · credit_json 求和 10,188（近 30 天 3,056），与设置页同一量级。

  结论：**积分只能从 credit_json 得到**；used/size 是上下文占用，跨会话求和
  没有业务含义（各会话窗口互相独立），故不参与任何积分口径。

口径边界（重要，别在界面上含糊）
  · credit_json 只有「消耗积分的调用」才有记录——用自己 API Key 的
    custom-local 模型、限免模型（倍率 x0.00）不产生积分记录；
    本机 232 个会话中 109 个有记录，其余多为 custom-local。
  · credit_json 的键是消息 UUID，值是该次调用的积分；无逐条时间戳，
    所以「本月 / 近 30 天」只能按会话的 updated_at 归期，属近似值。
  · **账户剩余积分、最近到期积分不在本地**（扫遍 ~/.workbuddy 无此字段），
    在服务端；要拿只能联网。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_DB = Path.home() / ".workbuddy" / "workbuddy.db"

# used/size 的含义（给调用方看的唯一解释，避免以后再被误读）
CONTEXT_NOTE = "used 为最后一次调用的输入 token 数，size 为该会话模型的上下文窗口"


def _sum_credits(raw: Optional[str]) -> tuple[float, int]:
    """解析 credit_json，返回 (积分合计, 调用次数)。

    credit_json 形如 {"<消息UUID>": 3.28, ...}；缺失或损坏时返回 (0, 0)，
    不抛异常——单个会话的坏数据不该让整张卡消失。
    """
    if not raw:
        return 0.0, 0
    try:
        d = json.loads(raw)
    except Exception:
        return 0.0, 0
    if not isinstance(d, dict):
        return 0.0, 0
    total = 0.0
    calls = 0
    for v in d.values():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v <= 0:
            continue
        total += float(v)
        calls += 1
    return total, calls


def _month_start_ms(now: Optional[datetime] = None) -> int:
    n = now or datetime.now()
    return int(datetime(n.year, n.month, 1).timestamp() * 1000)


def workbuddy_credits(db_path: Path = DEFAULT_DB,
                      since_ms: Optional[int] = None,
                      until_ms: Optional[int] = None,
                      now: Optional[datetime] = None) -> dict:
    """读 session_usage，返回积分汇总。

    返回结构：
      {
        "available": bool,            # 读到数据
        "note": str|None,             # 读不到时的原因
        "total_credit": float,        # 累计积分消耗（credit_json 求和）
        "credit_this_month": float,   # 本月（按会话 updated_at 归期，近似）
        "credit_last30": float,       # 近 30 天（同上）
        "sessions_total": int,        # session_usage 行数
        "sessions_billed": int,       # 有积分记录的会话数
        "calls": int,                 # 计积分的调用次数
        "sessions": [ {session_id, project, title, credit, calls, updated_at} ],
        "context_note": str,          # used/size 的准确含义
      }
    """
    db_path = Path(db_path)
    empty = {"available": False, "note": "未找到 workbuddy.db", "total_credit": 0.0,
             "credit_this_month": 0.0, "credit_last30": 0.0,         "sessions_total": 0,
             "sessions_billed": 0, "calls": 0, "sessions": [],
             "first_month": None, "context_note": CONTEXT_NOTE}
    if not db_path.exists():
        return empty

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        has_usage = con.execute(
            "select count(*) from sqlite_master where type='table' and name='session_usage'"
        ).fetchone()[0]
        if not has_usage:
            e = dict(empty)
            e["note"] = "数据库里没有 session_usage 表"
            return e
        rows = con.execute(
            "select u.session_id as sid, u.updated_at as upd, u.credit_json as cj, "
            "       s.cwd as cwd, s.title as title "
            "from session_usage u left join sessions s on s.id = u.session_id "
            "order by u.updated_at desc").fetchall()
    except sqlite3.Error as e:
        out = dict(empty)
        out["note"] = f"读取失败：{type(e).__name__}"
        return out
    finally:
        con.close()

    total = 0.0
    month = 0.0
    last30 = 0.0
    calls_all = 0
    billed = 0
    sessions = []
    first_ms = None
    m_start = _month_start_ms(now)
    d30 = int(((now or datetime.now()).timestamp()) * 1000) - 30 * 86400 * 1000
    for r in rows:
        upd = int(r["upd"] or 0)
        if since_ms is not None and upd < since_ms:
            continue
        if until_ms is not None and upd > until_ms:
            continue
        credit, calls = _sum_credits(r["cj"])
        if credit <= 0:
            continue
        billed += 1
        calls_all += calls
        total += credit
        if upd >= m_start:
            month += credit
        if upd >= d30:
            last30 += credit
        if upd > 0 and (first_ms is None or upd < first_ms):
            first_ms = upd
        cwd = r["cwd"] or ""
        sessions.append({
            "session_id": r["sid"],
            "project": cwd.rsplit("/", 1)[-1] if cwd else "(未知)",
            "title": r["title"] or "",
            "credit": round(credit, 2),
            "calls": calls,
            "updated_at": upd,
        })
    return {
        "available": bool(sessions),
        "note": None if sessions else "没有可用的积分记录（credit_json 全为空）",
        "total_credit": round(total, 2),
        "credit_this_month": round(month, 2),
        "credit_last30": round(last30, 2),
        # 累计从哪个月开始：让「累计 1 万」和设置页的当期读数能对上
        "first_month": (datetime.fromtimestamp(first_ms / 1000).strftime("%Y-%m")
                        if first_ms else None),
        "sessions_total": len(rows),
        "sessions_billed": billed,
        "calls": calls_all,
        "sessions": sessions,
        "context_note": CONTEXT_NOTE,
    }


def selftest() -> bool:
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {name}"
              + ("" if good else f"   得到 {got!r} / 期望 {want!r}"))

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="credits-test-"))
    db = tmp / "workbuddy.db"
    con = sqlite3.connect(str(db))
    con.execute("create table sessions (id text, cwd text, title text)")
    con.execute("create table session_usage (session_id text, used integer, "
                "size integer, updated_at integer, credit_json text)")
    con.executemany("insert into sessions values (?,?,?)", [
        ("s1", "/Users/x/proj-a", "会话一"),
        ("s2", "/Users/x/proj-b", None),
        ("s3", "/Users/x/proj-c", "无积分"),
    ])
    # 关键：used/size 是上下文占用，不能被当成积分
    con.executemany("insert into session_usage values (?,?,?,?,?)", [
        ("s1", 279882, 1000000, 1792730909720,
         '{"m1":3.28,"m2":0.5}'),
        # s2 无 title、有积分、更新时间最新：验证「标题缺失留空 + 排最前」
        ("s2", 500000, 300000, 1792850000000, '{"m5":1.0}'),
        ("s3", 98159, 262144, 1792800000000,
         '{"m3":50.79,"m4":6.17}'),
    ])
    con.commit()
    con.close()

    c = workbuddy_credits(db, now=datetime(2026, 10, 23, 18, 0))
    chk("总积分只取 credit_json（不把 used 当积分）", c["total_credit"], 61.74)
    chk("计积分会话数", c["sessions_billed"], 3)
    chk("session_usage 总行数单独统计", c["sessions_total"], 3)
    chk("计积分调用次数", c["calls"], 5)
    chk("按更新时间倒序（新的在前）", c["sessions"][0]["session_id"], "s2")
    chk("会话积分四舍五入到两位", c["sessions"][1]["credit"], 56.96)
    chk("项目名取自 cwd 末段", (c["sessions"][1]["project"], c["sessions"][2]["project"]),
        ("proj-c", "proj-a"))
    chk("标题缺失时留空", c["sessions"][0]["title"], "")
    chk("available 为真", c["available"], True)
    # used 合计（79.8 万）与积分（61.74）是两个量级，绝不能混用
    chk("used 不进入积分口径", c["total_credit"] != 279882 + 500000 + 98159, True)

    # 归期：三个会话更新时间都在 2026-10 → 本月/近 30 天均等于总计
    chk("本月按会话更新时间归期", c["credit_this_month"], 61.74)
    chk("近 30 天含本月会话", c["credit_last30"], 61.74)
    # 跨月：把 now 推到 12 月，本月应为 0 而近 30 天仍含（10-23 距今 > 30 天则为 0）
    c5 = workbuddy_credits(db, now=datetime(2026, 12, 5, 0, 0))
    chk("换月后本月归零、近 30 天也过期", (c5["credit_this_month"], c5["credit_last30"]),
        (0.0, 0.0))
    chk("给出累计起始月份（供界面注明跨月）", c["first_month"], "2026-10")

    # 时间过滤
    c2 = workbuddy_credits(db, since_ms=1792900000000)
    chk("since 过滤后无数据", (c2["sessions_billed"], c2["available"]), (0, False))

    # 坏数据的三种降级
    db_bad = tmp / "bad.db"
    con = sqlite3.connect(str(db_bad))
    con.execute("create table sessions (id text, cwd text, title text)")
    con.execute("create table session_usage (session_id text, used integer, "
                "size integer, updated_at integer, credit_json text)")
    con.executemany("insert into session_usage values (?,?,?,?,?)", [
        ("b1", 1, 1000, 1792800000000, "not-json"),
        ("b2", 1, 1000, 1792800000000, "[1,2,3]"),
        ("b3", 1, 1000, 1792800000000, '{"m":true,"n":"3.0","o":-2,"p":1.5}'),
    ])
    con.commit()
    con.close()
    cb = workbuddy_credits(db_bad)
    chk("坏 JSON / 非字典 / 非数值全部跳过，只留 1.5",
        (cb["total_credit"], cb["calls"]), (1.5, 1))

    # 数据库不存在时的降级
    c3 = workbuddy_credits(tmp / "no-such.db")
    chk("缺库时给出原因而非崩溃", (c3["available"], c3["note"]), (False, "未找到 workbuddy.db"))

    # 有库无表
    db2 = tmp / "empty.db"
    con = sqlite3.connect(str(db2)); con.execute("create table x (a)"); con.commit(); con.close()
    c4 = workbuddy_credits(db2)
    chk("缺表时说明原因", c4["note"], "数据库里没有 session_usage 表")

    print("\n  " + ("积分模块自测全部通过 ✓" if ok else "存在失败项 ✗"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
