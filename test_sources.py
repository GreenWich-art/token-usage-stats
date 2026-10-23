#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_sources.py —— 三源直读（Codex / Claude Code / DeepSeek Harness）的回归测试

为什么用合成夹具而不是真实数据
  真实会话属受保护数据，不该进测试、也不该进仓库；而解析器的正确性来自
  口径（哪个字段含哪个），跟具体内容无关。所以这里用**我按实测结构手写的假文件**
  驱动真正的采集函数，断言事件数、四项 token、来源标记、模型名、去重行为。

口径依据（2026-10-23 与 HUD 账本逐文件对账）
  codex   Δ +0.0%   input_tokens 含缓存 → 扣；output 含 reasoning → 不另加
  claude  Δ 0       input 不含缓存；同 message.id 多行取"用量最大的一行"
  dsh     Δ +0.8%   inputTokens 净额；cacheReadTokens 独立；reasoning 已含在 output

用法：python3 test_sources.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wb_token_semantics as sem  # noqa: E402

OK = True


def chk(name: str, got, want) -> None:
    global OK
    good = got == want
    OK = OK and good
    print(f"  [{'PASS' if good else 'FAIL'}] {name}"
          + ("" if good else f"   得到 {got!r} / 期望 {want!r}"))


def write(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------- Codex
def case_codex(tmp: Path) -> None:
    root = tmp / "codex"
    # 文件名里的 UUID 会被当作 session id
    f = root / "2026" / "10" / "13" / \
        "rollout-2026-10-13T22-25-05-01a135a7-1784-79b0-949f-49d1a02a90cf.jsonl"
    ts = "2026-10-13T22:25:05.000Z"
    write(f, [
        {"timestamp": ts, "type": "session_meta",
         "payload": {"id": "01a135a7-1784-79b0-949f-49d1a02a90cf", "cwd": "/Users/x/我的项目"}},
        {"timestamp": ts, "type": "turn_context", "payload": {"model": "gpt-6-luna"}},
        {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800,
                                 "output_tokens": 50, "reasoning_output_tokens": 10}}}},
        {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 2000, "cached_input_tokens": 1500,
                                 "output_tokens": 70, "reasoning_output_tokens": 20}}}},
        # 同一轮还会写一条 turn.completed，不能重复计（故意给夸张的值，误算会立刻暴露）
        {"timestamp": ts, "type": "turn.completed",
         "usage": {"input_tokens": 99999, "cached_input_tokens": 99999, "output_tokens": 999}},
    ])
    stats = {}
    evs = list(sem.collect_codex(root, stats=stats))
    chk("codex 事件数（只取 token_count，不吃 turn.completed）", len(evs), 2)
    chk("codex 输入已扣缓存", sum(e["tin"] for e in evs), (1000 - 800) + (2000 - 1500))
    chk("codex 缓存读", sum(e["cread"] for e in evs), 800 + 1500)
    chk("codex 输出（reasoning 不另加）", sum(e["tout"] for e in evs), 50 + 70)
    chk("codex agent 名", evs[0]["agent"], "codex-model:gpt-6-luna")
    chk("codex session id 来自文件名", evs[0]["session"], "01a135a7-1784-79b0-949f-49d1a02a90cf")
    chk("codex 项目名带来源前缀", evs[0]["project"], "codex/我的项目")
    chk("codex stats 记录文件数与事件数",
        (stats["codex"]["files"], stats["codex"]["events"]), (1, 2))

    # 老版本只有 turn.completed → 必须回退
    root2 = tmp / "codex2"
    write(root2 / "2026/10/14/rollout-2026-10-14T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
          [{"timestamp": ts, "type": "turn.completed",
            "usage": {"input_tokens": 500, "cached_input_tokens": 300, "output_tokens": 40}}])
    evs2 = list(sem.collect_codex(root2))
    chk("codex 无 token_count 时回退 turn.completed",
        (len(evs2), evs2[0]["tin"], evs2[0]["cread"], evs2[0]["tout"]), (1, 200, 300, 40))


# ---------------------------------------------------------------- Claude
def case_claude(tmp: Path) -> None:
    root = tmp / "claude"
    f = root / "-Users-x-proj" / "86b1cd78-144a-45ae-b8be-93422993e648.jsonl"

    def msg(mid, ts, i, cr, cw, o, model="claude-sonnet-5.5"):
        return {"type": "assistant", "sessionId": "86b1cd78-144a-45ae-b8be-93422993e648",
                "cwd": "/Users/x/proj", "timestamp": ts,
                "message": {"id": mid, "model": model, "usage": {
                    "input_tokens": i, "cache_read_input_tokens": cr,
                    "cache_creation_input_tokens": cw, "output_tokens": o}}}

    write(f, [
        msg("m1", "2026-10-20T10:00:00.000Z", 0, 0, 0, 0),        # 中间态，全 0
        msg("m1", "2026-10-20T10:00:01.000Z", 936, 25920, 0, 115),  # 真实值
        msg("m1", "2026-10-20T10:00:02.000Z", 936, 25920, 0, 115),  # 又一遍
        msg("m2", "2026-10-20T10:05:00.000Z", 200, 8000, 100, 60),
    ])
    # 子代理目录默认排除
    write(root / "-Users-x-proj" / "86b1cd78-144a-45ae-b8be-93422993e648" / "subagents" / "a1.jsonl",
          [msg("m9", "2026-10-20T11:00:00.000Z", 1, 2, 3, 4)])

    stats = {}
    evs = list(sem.collect_claude(root, stats=stats))
    chk("claude 事件数（同 id 多行只算一次，子代理默认排除）", len(evs), 2)
    chk("claude 输入不扣缓存（该字段本就是净额）", sum(e["tin"] for e in evs), 936 + 200)
    chk("claude 缓存读", sum(e["cread"] for e in evs), 25920 + 8000)
    chk("claude 缓存写", sum(e["cwrite"] for e in evs), 100)
    chk("claude 输出", sum(e["tout"] for e in evs), 115 + 60)
    chk("claude agent 名", evs[0]["agent"], "claude-model:claude-sonnet-5.5")
    chk("claude 项目名带来源前缀", evs[0]["project"], "claude/proj")

    evs_sub = list(sem.collect_claude(root, include_subagents=True))
    chk("claude 显式开关可纳入子代理", len(evs_sub), 3)


# ---------------------------------------------------------------- dsh
def case_dsh(tmp: Path) -> None:
    root = tmp / "dsh"
    sess = root / "--Users-x-proj--" / "session-2634add7-ab1b-40ef-aa7f-f0b3d342063f"
    lines = [
        {"type": "session", "version": 4, "id": "2634add7-ab1b-40ef-aa7f-f0b3d342063f",
         "createdAt": 1792582074479, "cwd": "/Users/x/proj", "delegationDepth": 0},
        # 第 1 条：usage 与 stream 里各写一份，只能算一次
        {"type": "assistant/message", "seq": 1, "time": 1792586338848, "data": {
            "message": {"source": {"kind": "model", "model": "deepseek-v4.1-flash-free"}},
            "usage": {"inputTokens": 12660, "outputTokens": 273,
                      "totalTokens": 12933, "reasoningTokens": 123},
            "stream": [{"type": "chunk", "chunk": {"usage": {
                "inputTokens": 12660, "outputTokens": 273, "reasoningTokens": 123}}}]}},
        # 第 2 条：带 cacheReadTokens
        {"type": "assistant/message", "seq": 2, "time": 1792586341739, "data": {
            "usage": {"inputTokens": 232, "outputTokens": 270, "totalTokens": 13430,
                      "cacheReadTokens": 12928, "reasoningTokens": 99}}},
    ]
    write(sess / "session.v4.jsonl", lines)

    stats = {}
    evs = list(sem.collect_dsh(root, stats=stats))
    chk("dsh 事件数（data.usage 与 stream 只算一次）", len(evs), 2)
    chk("dsh 输入", sum(e["tin"] for e in evs), 12660 + 232)
    chk("dsh 缓存读来自 cacheReadTokens", sum(e["cread"] for e in evs), 12928)
    chk("dsh 输出（reasoning 已含）", sum(e["tout"] for e in evs), 273 + 270)
    chk("dsh agent 名沿用最后见到的模型", evs[0]["agent"],
        "deepseek-model:deepseek-v4.1-flash-free")
    chk("dsh 项目名带来源前缀", evs[0]["project"], "dsh/proj")
    chk("dsh 会话 id 取自 header", evs[0]["session"], "2634add7-ab1b-40ef-aa7f-f0b3d342063f")

    # zstd 真实解压链路（本机有 zstd CLI 才测）
    if shutil.which("zstd"):
        zsrc = sess / "session.v4.jsonl"
        zdst = sess / "session.v5.jsonl.zstd"
        subprocess.run(["zstd", "-q", "-f", str(zsrc), "-o", str(zdst)], check=False)
        if zdst.exists():
            zsrc.unlink()                       # 只留 zstd，强制走解压路径
            evs_z = list(sem.collect_dsh(root))
            chk("dsh zstd 多帧解压后结果一致",
                (len(evs_z), sum(e["tin"] for e in evs_z), sum(e["cread"] for e in evs_z)),
                (2, 12660 + 232, 12928))
        else:
            print("  [SKIP] zstd 压缩失败，跳过解压测试")
    else:
        print("  [SKIP] 本机无 zstd CLI，跳过解压测试")


# ---------------------------------------------------------------- 合并
def case_all(tmp: Path) -> None:
    roots = {"workbuddy": tmp / "no-such-dir",
             "codex": tmp / "codex", "claude": tmp / "claude", "dsh": tmp / "dsh",
             "ledger": tmp / "no-such.db"}
    stats = {}
    evs = list(sem.collect_all(roots, stats=stats))
    chk("collect_all 合并三源", len(evs), 2 + 2 + 2)
    chk("不存在的目录自动跳过（workbuddy 没报错）", "workbuddy" not in stats, True)
    agg = sem.aggregate(evs)
    chk("聚合总计 = 逐事件求和",
        agg["totals"]["total"],
        sum(e["tin"] + e["cread"] + e["tout"] + e["cwrite"] + e["reasoning"] for e in evs))
    clients = {e["client"] for e in evs}
    chk("三个客户端都被标记", clients, {"codex", "claude", "deepseek"})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="test-sources-"))
    print("=" * 74)
    print("三源直读回归测试（合成夹具，不读任何真实会话）")
    print("=" * 74)
    try:
        print("\n[1] Codex")
        case_codex(tmp)
        print("\n[2] Claude Code")
        case_claude(tmp)
        print("\n[3] DeepSeek Harness")
        case_dsh(tmp)
        print("\n[4] 四源合并")
        case_all(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("全部通过 ✓" if OK else "存在失败项 ✗"))
    return 0 if OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
