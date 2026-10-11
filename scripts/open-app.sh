#!/usr/bin/env bash
# サーバーが起動していなければバックグラウンドで起動し、ブラウザで開く。
# Claude Code の Skill から、または手動でダブルクリック相当に叩くための入口。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="http://127.0.0.1:8000/"
LOG_DIR="$REPO_DIR/data/logs"

is_up() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$URL" 2>/dev/null || true)"
  [ "$code" = "200" ]
}

if ! is_up; then
  mkdir -p "$LOG_DIR"
  echo "サーバーが起動していないため、起動します…" >&2
  (
    cd "$REPO_DIR"
    nohup .venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 \
      >>"$LOG_DIR/server.log" 2>>"$LOG_DIR/server.error.log" &
    disown
  )

  ready=0
  for _ in $(seq 1 20); do
    if is_up; then
      ready=1
      break
    fi
    sleep 0.5
  done

  if [ "$ready" -ne 1 ]; then
    echo "起動に失敗しました。ログを確認してください: $LOG_DIR/server.error.log" >&2
    tail -n 20 "$LOG_DIR/server.error.log" >&2 2>/dev/null || true
    exit 1
  fi
fi

open "$URL"
echo "変換アプリを開きました: $URL"
