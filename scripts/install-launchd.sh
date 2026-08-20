#!/usr/bin/env bash
# 봇 2개와 Dev Tunnel 을 launchd 에 등록한다. 죽으면 재시작되고, 재부팅 후에도 뜬다.
#
#   ./scripts/install-launchd.sh "$(which devtunnel)"
#   ./scripts/install-launchd.sh "$(which devtunnel)" teams-agents.<리전>   # ID 직접 지정
#   ./scripts/install-launchd.sh --uninstall
#
# launchd 는 최소 PATH 로 실행되므로 devtunnel 의 절대 경로가 필요하다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
# 터널 ID 에는 리전 접미사가 붙는다 (예: teams-agents.<리전>).
# "teams-agents" 만 쓰면 devtunnel 이 "그런 터널 없음" 으로 조용히 실패한다.
# 두 번째 인자로 넘길 수 있고, 없으면 devtunnel list 에서 찾는다.
TUNNEL_ID="${2:-}"
LABELS=(com.example.teams.tunnel com.example.teams.roulette com.example.teams.vote)
DOMAIN="gui/$(id -u)"

unload_all () {
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  done
}

if [[ "${1:-}" == "--uninstall" ]]; then
  unload_all
  for label in "${LABELS[@]}"; do rm -f "$AGENTS_DIR/$label.plist"; done
  echo "✅ 해제 완료 (plist 3개 제거)"
  exit 0
fi

DEVTUNNEL="${1:-}"
if [[ -z "$DEVTUNNEL" || ! -x "$DEVTUNNEL" ]]; then
  echo "❌ devtunnel 의 절대 경로가 필요하다:" >&2
  echo "     ./scripts/install-launchd.sh \"\$(which devtunnel)\"" >&2
  exit 2
fi

if [[ -z "$TUNNEL_ID" ]]; then
  TUNNEL_ID="$("$DEVTUNNEL" list 2>/dev/null | grep -Eo 'teams-agents\.[a-z0-9]+' | head -1)"
fi
if [[ -z "$TUNNEL_ID" ]]; then
  echo "❌ teams-agents 터널을 찾지 못했다. 확인:" >&2
  echo "     devtunnel list" >&2
  echo "   전체 ID(리전 접미사 포함)를 두 번째 인자로 넘긴다:" >&2
  echo "     ./scripts/install-launchd.sh \"\$(which devtunnel)\" teams-agents.<리전>" >&2
  exit 1
fi
echo "터널 ID: $TUNNEL_ID"

PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "❌ venv 가 없다. STEP 4-1 을 먼저 실행한다: $PYTHON" >&2; exit 1; }

for agent in roulette-agent vote-agent; do
  [[ -f "$ROOT/$agent/src/.env" ]] || { echo "❌ $agent/src/.env 이 없다. STEP 4-2 를 먼저 한다." >&2; exit 1; }
done

mkdir -p "$AGENTS_DIR" "$ROOT/logs"

# $1=label  $2=작업디렉터리  $3=로그이름  $4.. = 실행할 명령
write_plist () {
  local label="$1" workdir="$2" logname="$3"; shift 3
  local plist="$AGENTS_DIR/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0">'
    echo '<dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key>'
    echo '  <array>'
    for arg in "$@"; do echo "    <string>$arg</string>"; done
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>$workdir</string>"
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo '  <key>ThrottleInterval</key><integer>10</integer>'
    echo "  <key>StandardOutPath</key><string>$ROOT/logs/$logname.log</string>"
    echo "  <key>StandardErrorPath</key><string>$ROOT/logs/$logname.log</string>"
    echo '  <key>EnvironmentVariables</key>'
    echo '  <dict>'
    echo '    <key>PYTHONUNBUFFERED</key><string>1</string>'
    echo "    <key>PATH</key><string>$(dirname "$DEVTUNNEL"):/usr/bin:/bin:/usr/sbin:/sbin</string>"
    echo '  </dict>'
    echo '</dict>'
    echo '</plist>'
  } > "$plist"
  plutil -lint "$plist" > /dev/null
  echo "   $plist"
}

unload_all

echo "plist 작성:"
write_plist com.example.teams.tunnel   "$ROOT"                    tunnel   "$DEVTUNNEL" host "$TUNNEL_ID"
write_plist com.example.teams.roulette "$ROOT/roulette-agent/src" roulette "$PYTHON" app.py
write_plist com.example.teams.vote     "$ROOT/vote-agent/src"     vote     "$PYTHON" app.py

echo "등록:"
for label in "${LABELS[@]}"; do
  launchctl bootstrap "$DOMAIN" "$AGENTS_DIR/$label.plist"
  echo "   $label"
done

echo
echo "✅ 등록 완료. 상태 확인:"
echo "     launchctl list | grep example.teams"
echo "   로그:"
echo "     tail -f $ROOT/logs/*.log"
echo "   코드 수정 후 재시작:"
echo "     launchctl kickstart -k $DOMAIN/com.example.teams.roulette"
