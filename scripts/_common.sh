# 세 start 스크립트가 공유하는 함수. 직접 실행하지 않고 source 한다.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
TUNNEL_NAME="teams-agents"

# TERM 후 이만큼(0.15초 × 10 = 1.5초) 기다렸다가 -9 로 확인 사살한다.
GRACE_TICKS=10
TICK=0.15

say() { printf '  %s\n' "$*"; }
die() { printf '❌ %s\n' "$*" >&2; exit 1; }
nap() { perl -e "select(undef,undef,undef,$1)"; }

# ── 종료 ──────────────────────────────────────────────────────
# TERM 을 먼저 보내고, 남아 있으면 KILL(-9) 한다.
#
# 왜 -9 까지 가는가: uvicorn 이 asyncio.sleep 중일 때 SIGTERM 을 늦게(또는 아예)
# 처리하지 않아 프로세스가 살아남는 일이 반복됐다. 포트가 잡혀 있으면 새 프로세스가
# "address already in use" 로 뜬 직후 죽는데, 로그에는 "started successfully" 가
# 먼저 찍혀서 성공처럼 보인다.
#
# -9 가 안전한 이유: vote-agent 는 SQLite 트랜잭션마다 커밋하므로 중간 상태가 남지
# 않고, roulette-agent 는 아무것도 저장하지 않는다.
alive_of() {
    local out=""
    for p in "$@"; do
        [ -n "$p" ] && kill -0 "$p" 2>/dev/null && out="$out $p"
    done
    printf '%s' "${out# }"
}

stop_pids() {
    local label="$1"; shift
    local pids="$*"
    pids="$(alive_of $pids)"
    if [ -z "$pids" ]; then
        say "· $label — 실행 중인 것 없음"
        return 0
    fi

    kill $pids 2>/dev/null || true
    local i=0
    while [ "$i" -lt "$GRACE_TICKS" ]; do
        [ -z "$(alive_of $pids)" ] && { say "· $label — TERM 으로 종료 (PID $pids)"; return 0; }
        nap "$TICK"
        i=$((i + 1))
    done

    local left
    left="$(alive_of $pids)"
    if [ -n "$left" ]; then
        kill -9 $left 2>/dev/null || true
        nap 0.3
        say "· $label — TERM 무응답, KILL -9 로 종료 (PID $left)"
    else
        say "· $label — 종료 (PID $pids)"
    fi
}

stop_port() {
    local port="$1"
    stop_pids "포트 $port" "$(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')"

    # 포트가 실제로 비었는지 확인한다. 안 비면 새 프로세스가 조용히 죽는다.
    local i=0
    while [ "$i" -lt 15 ]; do
        lsof -t -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || return 0
        nap 0.2
        i=$((i + 1))
    done
    die "포트 $port 가 아직 잡혀 있다.  lsof -nP -iTCP:$port -sTCP:LISTEN"
}

# comm(실행 파일 경로)이 devtunnel 로 끝나는 프로세스만 잡는다.
# 명령줄 문자열로 찾으면 이 스크립트 자신이나 셸 래퍼까지 걸린다.
tunnel_pids() {
    ps -axo pid=,comm= | awk '$2 ~ /devtunnel$/ { printf "%s ", $1 }'
}

stop_tunnel() { stop_pids "devtunnel host" "$(tunnel_pids)"; }

# ── 사전 확인 ──────────────────────────────────────────────────
devtunnel_bin() {
    local found
    found="$(command -v devtunnel 2>/dev/null)"
    [ -n "$found" ] && { printf '%s' "$found"; return 0; }
    for p in "$HOME/bin/devtunnel" /usr/local/bin/devtunnel /opt/homebrew/bin/devtunnel; do
        [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    return 1
}

# 터널 ID 에는 리전 접미사가 붙는다 (teams-agents.jpe1 등).
# 조립하면 조용히 실패하므로 반드시 조회해서 쓴다.
resolve_tunnel_id() {
    "$1" list 2>/dev/null | grep -Eo "${TUNNEL_NAME}\.[a-z0-9]+" | head -1
}

require_venv() {
    [ -x "$PYTHON" ] || die "venv 가 없다: $PYTHON
   python3.13 -m venv .venv && ./.venv/bin/pip install -r roulette-agent/src/requirements.txt"
}

require_env() {
    [ -f "$ROOT/$1/src/.env" ] || die "$1/src/.env 가 없다.
   cp $1/src/.env.example $1/src/.env 로 만들고 값을 채운 뒤 ./scripts/check-env.sh 로 확인"
}

# ── 실행 ──────────────────────────────────────────────────────
# 기본은 포그라운드다 (로그를 그대로 보고 Ctrl+C 로 끈다).
# --bg 를 주면 nohup 으로 떼어내고 logs/ 에 기록한다.
# BG 를 전역으로 설정한다.
#
# `BG="$(parse_background "$1")"` 처럼 명령 치환으로 쓰면 안 된다 —
# 안에서 die 를 호출해도 **서브셸만** 종료되어 잘못된 옵션이 그대로 통과한다.
parse_args() {
    BG=0
    case "${1:-}" in
        "") ;;
        --bg|-b|--background) BG=1 ;;
        *) die "모르는 옵션: $1   (쓸 수 있는 것: --bg)" ;;
    esac
    [ "$#" -le 1 ] || die "인자가 너무 많다: $*"
}

run_or_background() {
    local name="$1" background="$2"; shift 2
    if [ "$background" = "1" ]; then
        mkdir -p "$ROOT/logs"
        local log="$ROOT/logs/$name.log"
        nohup "$@" >>"$log" 2>&1 &
        local pid=$!
        disown 2>/dev/null || true
        nap 0.8
        if kill -0 "$pid" 2>/dev/null; then
            printf '✅ %s 백그라운드 시작 (PID %s)\n   로그: tail -f %s\n' "$name" "$pid" "$log"
        else
            printf '❌ %s 가 즉시 종료됐다. 로그를 확인한다:\n   tail -20 %s\n' "$name" "$log" >&2
            exit 1
        fi
    else
        printf '✅ %s 시작 — Ctrl+C 로 종료\n\n' "$name"
        exec "$@"
    fi
}
