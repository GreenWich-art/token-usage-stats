#!/bin/bash
# 一键启动：起本地服务 → 等它就绪 → 打开浏览器
# 关闭此终端窗口（或按 Ctrl-C）即停止服务。

cd "$(dirname "$0")" || exit 1
PORT=8791
URL="http://127.0.0.1:$PORT/"

# 已经在跑就直接开页面，避免重复启动
if curl -s -m 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "服务已在运行，打开页面…"
  open "$URL"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。"
  echo "请先安装：从 https://www.python.org/downloads/ 下载，或执行 brew install python3"
  read -r -p "按回车关闭…" _
  exit 1
fi

cleanup() {
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM

python3 token-usage-server.py --port "$PORT" &
PID=$!

# 最多等 12 秒（首次要扫一遍历史会话，比平时慢）
for _ in $(seq 1 30); do
  if curl -s -m 1 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    open "$URL"
    echo "已打开 $URL（关闭本窗口即停止服务）"
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "服务启动失败，请查看上面的提示。"
    read -r -p "按回车关闭…" _
    exit 1
  fi
  sleep 0.4
done

wait "$PID"
