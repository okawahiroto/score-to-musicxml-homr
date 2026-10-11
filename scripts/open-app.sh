#!/usr/bin/env bash
# 本アプリのサーバーを探し、なければ起動して、ブラウザで開く。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_DIR/data/logs"
PORT=8765
URL="http://127.0.0.1:$PORT/"
OPEN_CMD="${OPEN_CMD:-open}"

# /api/health が本アプリの名前を返すかで判定する。
# 別のアプリが 200 を返しても、自分のサーバーとは見なさない。
is_ours() {
  curl -s --max-time 2 "${URL}api/health" 2>/dev/null | grep -q 'score-to-musicxml-homr'
}

# 何かがその番号で待ち受けているか(別のアプリの可能性がある)。
is_listening() {
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

open_app() {
  "$OPEN_CMD" "$URL"
  echo "変換アプリを開きました: $URL"
  exit 0
}

if is_ours; then open_app; fi

if is_listening; then
  echo "${PORT}番は別のアプリが使用中です。そのアプリを終了してから、もう一度開いてください。" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
echo "サーバーを起動します…" >&2
pid="$(
  cd "$REPO_DIR"
  nohup .venv/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
    >>"$LOG_DIR/server.log" 2>>"$LOG_DIR/server.error.log" &
  echo $!
)"

for _ in $(seq 1 120); do
  if is_ours; then open_app; fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "サーバーを起動できませんでした。ログ: $LOG_DIR/server.error.log" >&2
    tail -n 20 "$LOG_DIR/server.error.log" >&2 2>/dev/null || true
    exit 1
  fi
  sleep 0.5
done

# 生きたまま時間切れ: 起動が遅いだけなので止めない。
echo "起動に時間がかかっています。少し待ってからもう一度開いてください。" >&2
echo "ログ: $LOG_DIR/server.error.log" >&2
exit 1
