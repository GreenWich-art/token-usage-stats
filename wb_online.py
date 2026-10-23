#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_online.py — 可选的联网读取（账户余额 / 剩余积分）

为什么是"用户自填端点"
  WorkBuddy 的账户剩余积分、DSH 的账户余额都在服务端，本地没有缓存。
  但我**不逆向厂商 API**：端点、请求头、字段路径全部由用户在配置文件里填，
  默认关闭。用户能从自己账号的后台/开发者工具看到这些信息，填进来即可。

配置文件
  ~/.token-usage-stats/online.json     （不存在或 enabled=false 时完全不联网）
  结构：
    {
      "enabled": false,
      "timeout_sec": 8,
      "endpoints": {
        "workbuddy_credits": {
          "url": "https://example.com/api/credits",
          "method": "GET",
          "headers": {"Authorization": "Bearer <你的令牌>"},
          "fields": {"remaining": "data.credits.remaining",
                     "expiring":  "data.credits.expiringSoon"}
        },
        "dsh_balance": {
          "url": "", "method": "GET", "headers": {},
          "fields": {"balance": "data.balance", "used": "data.usedAmount"}
        }
      }
    }

安全与隐私
  · 只有用户显式 enabled=true 才发请求；
  · headers 里的令牌只从配置文件读，**不打印、不写日志、不出现在任何错误信息里**；
  · 错误信息只报"哪个端点失败、HTTP 状态/异常类型"，不带 URL 查询串与响应体。
"""
from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path.home() / ".token-usage-stats" / "online.json"

# 首次运行时落盘的模板：**保留占位符**，用户看得懂该在哪个键里填什么。
# 说明文字放在 "_说明" 里，不影响解析（load_config 只取它认识的键）。
CONFIG_TEMPLATE = {
    "enabled": False,
    "plan_quota": False,
    "timeout_sec": 8,
    "api_keys": {
        "deepseek": "",
    },
    "endpoints": {},
    "_说明": [
        "本文件只保存在本机，权限 600；应用不会上传、不会打印其中的 Key。",
        "plan_quota：改为 true 后，从厂商接口读取已接入的 Coding Plan 额度"
        "（智谱 GLM、Kimi）。端点已内置，Key 自动取自本机已配置的模型。",
        "api_keys：按量计费渠道的余额需要 API Key。在对应键的值里填入即可，"
        "例如 \"deepseek\": \"sk-...\"。保留键名、只改引号里的内容；保存后自动生效。",
        "endpoints：账户余额 / 剩余积分（可选）。自行填写 url、headers、fields，"
        "字段路径用点号嵌套，如 data.credits.remaining。",
    ],
}

# 每个端点产出的行长什么样（UI 直接渲染）：
#   {"client","kind","label","value","unit","currency","source","fetched_at","note"}


def ensure_config(path: Path = CONFIG_PATH) -> bool:
    """首次运行时生成带占位符的配置模板，返回 True 表示新建了文件。

    已存在则**原样保留**——绝不覆盖用户填过的 Key 或改动过的开关。
    文件里有 Key，因此权限固定 600（仅本人可读写）。
    """
    p = Path(path)
    if p.exists():
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 用 os.open 带 0o600 直接创建，文件从诞生起就只有本人可读，
        # 不存在「先 644 再 chmod」之间被其他进程读到的窗口。
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n")
        return True
    except Exception:
        return False


def _dig(obj, path: str):
    """按 'a.b.0.c' 取嵌套值；任一层缺失返回 None。"""
    cur = obj
    for part in str(path).split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            return None
    return None


def load_config(path: Path = CONFIG_PATH) -> dict:
    """读配置；任何异常都按"未启用"处理，绝不因配置写错而崩。"""
    p = Path(path)
    if not p.exists():
        return {"enabled": False, "note": "未配置文件，联网读取未启用",
                "endpoints": {}, "rows": [], "errors": []}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"enabled": False, "note": f"配置文件解析失败：{type(e).__name__}",
                "endpoints": {}, "rows": [], "errors": []}
    if not isinstance(cfg, dict):
        return {"enabled": False, "note": "配置文件不是 JSON 对象",
                "endpoints": {}, "rows": [], "errors": []}
    cfg.setdefault("endpoints", {})
    cfg.setdefault("timeout_sec", 8)
    return cfg


def _fetch(url: str, method: str, headers: dict, timeout: float):
    req = urllib.request.Request(url, method=(method or "GET").upper())
    for k, v in (headers or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310（用户自填地址）
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_online(cfg: Optional[dict] = None) -> dict:
    """按配置拉取账户级数据。任何失败都转为 rows 里的 note，不抛异常。"""
    if cfg is None:
        cfg = load_config()
    out = {"enabled": bool(cfg.get("enabled")), "rows": [], "errors": [],
           "note": cfg.get("note")}
    if not out["enabled"]:
        return out

    timeout = float(cfg.get("timeout_sec") or 8)
    eps = cfg.get("endpoints") or {}
    import time
    now = int(time.time())

    specs = [
        ("workbuddy", "workbuddy_credits", "积分"),
        ("dsh", "dsh_balance", "余额"),
    ]
    for client, key, unit in specs:
        ep = eps.get(key) or {}
        url = (ep.get("url") or "").strip()
        fields = ep.get("fields") or {}
        if not url or not isinstance(fields, dict) or not fields:
            out["errors"].append(f"{client}：端点或字段未配置，已跳过")
            continue
        try:
            data = _fetch(url, ep.get("method") or "GET", ep.get("headers") or {}, timeout)
        except urllib.error.HTTPError as e:
            out["errors"].append(f"{client}：HTTP {e.code}")
            continue
        except Exception as e:                # 超时 / DNS / TLS / JSON 解析失败
            out["errors"].append(f"{client}：{type(e).__name__}")
            continue
        got_any = False
        for label, path in fields.items():
            v = _num(_dig(data, str(path)))
            if v is None:
                continue
            got_any = True
            out["rows"].append({
                "client": client, "kind": key, "label": str(label),
                "value": v, "unit": unit, "currency": None,
                "source": "online", "fetched_at": now, "note": None,
            })
        if not got_any:
            out["errors"].append(f"{client}：响应里找不到配置的字段")

    return out


# ---------------------------------------------------------------- 自测
def selftest() -> bool:
    ok = True

    def chk(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {name}"
              + ("" if good else f"   得到 {got!r} / 期望 {want!r}"))

    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):                                # noqa: N802
            body = json.dumps({"data": {"credits": {"remaining": 1234.5,
                                                    "expiringSoon": 88},
                                        "balance": 56.78, "usedAmount": 12.3}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):                       # 静音
            return

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="online-test-"))
    cfg_path = tmp / "online.json"

    # 0) 首次运行生成带占位符的模板；权限 600；已存在不覆盖
    tpl = tmp / "new" / "online.json"
    chk("首次运行创建配置模板", ensure_config(tpl), True)
    saved = json.loads(tpl.read_text(encoding="utf-8"))
    chk("模板保留 api_keys.deepseek 空占位", saved["api_keys"].get("deepseek"), "")
    chk("模板带填写说明", isinstance(saved.get("_说明"), list) and len(saved["_说明"]) >= 3, True)
    chk("模板权限为 600（含 Key 的文件）",
        oct(stat.S_IMODE(tpl.stat().st_mode)), "0o600")
    chk("已存在时不再覆盖（保护用户已填的 Key）", ensure_config(tpl), False)
    tpl.write_text(json.dumps({"api_keys": {"deepseek": "sk-keepme"}}), encoding="utf-8")
    ensure_config(tpl)
    chk("覆盖检查：用户内容仍在",
        json.loads(tpl.read_text())["api_keys"]["deepseek"], "sk-keepme")

    # 1) 未启用 / 无配置 → 不联网
    chk("无配置时不联网", fetch_online(load_config(cfg_path))["enabled"], False)
    cfg_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    chk("enabled=false 时不联网", fetch_online(load_config(cfg_path))["enabled"], False)

    # 2) 正常路径
    cfg_path.write_text(json.dumps({
        "enabled": True, "timeout_sec": 5,
        "endpoints": {
            "workbuddy_credits": {"url": f"http://127.0.0.1:{port}/credits",
                                  "method": "GET",
                                  "headers": {"Authorization": "Bearer secret"},
                                  "fields": {"剩余积分": "data.credits.remaining",
                                             "近期到期": "data.credits.expiringSoon"}},
            "dsh_balance": {"url": f"http://127.0.0.1:{port}/balance",
                            "fields": {"账户余额": "data.balance",
                                       "已用金额": "data.usedAmount"}},
        }}), encoding="utf-8")
    r = fetch_online(load_config(cfg_path))
    chk("启用后产出 4 行", len(r["rows"]), 4)
    chk("嵌套字段路径取到值", sorted(x["value"] for x in r["rows"]),
        [12.3, 56.78, 88.0, 1234.5])
    chk("行里带客户端与单位", {(x["client"], x["unit"]) for x in r["rows"]},
        {("workbuddy", "积分"), ("dsh", "余额")})
    chk("无错误", r["errors"], [])

    # 3) 端点不可达 → 降级为错误说明，不崩
    cfg2 = tmp / "bad.json"
    cfg2.write_text(json.dumps({
        "enabled": True, "timeout_sec": 2,
        "endpoints": {"workbuddy_credits": {
            "url": "http://127.0.0.1:1/nope", "fields": {"x": "a.b"}},
            "dsh_balance": {"url": "", "fields": {}}}}), encoding="utf-8")
    r2 = fetch_online(load_config(cfg2))
    chk("不可达时返回错误而非抛异常", len(r2["errors"]) >= 1 and r2["rows"], [])
    chk("错误信息不带 URL", all("127.0.0.1" not in e for e in r2["errors"]), True)

    # 4) 字段取不到 → 明确说明
    cfg3 = tmp / "nofield.json"
    cfg3.write_text(json.dumps({
        "enabled": True,
        "endpoints": {"dsh_balance": {"url": f"http://127.0.0.1:{port}/x",
                                      "fields": {"余额": "data.nope"}}}}), encoding="utf-8")
    r3 = fetch_online(load_config(cfg3))
    chk("字段缺失时说明原因", sorted(r3["errors"]),
        ["dsh：响应里找不到配置的字段", "workbuddy：端点或字段未配置，已跳过"])

    # 5) 配置损坏
    cfg4 = tmp / "broken.json"
    cfg4.write_text("{ 不是 JSON", encoding="utf-8")
    r4 = load_config(cfg4)
    chk("配置损坏按未启用处理", (r4["enabled"], "解析失败" in r4["note"]), (False, True))

    srv.shutdown()
    print("\n  " + ("联网模块自测全部通过 ✓" if ok else "存在失败项 ✗"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if selftest() else 1)
