#!/usr/bin/env bash
# Teams 앱 패키지(zip)를 만든다.
#
#   ./scripts/build-package.sh roulette-agent
#
# src/.env 에서 BOT_ID / TEAMS_APP_ID / APP_NAME_SUFFIX 를 읽어 매니페스트의
# 플레이스홀더를 채운다. TEAMS_APP_ID 가 비어 있으면 GUID 를 생성해 .env 에
# 적어 넣는다. 앱 신원이 바뀌면 Teams 에서 재설치를 해야 하므로, 한 번 정해진
# GUID 는 재실행해도 그대로 유지된다.
set -euo pipefail

AGENT="${1:-}"
if [[ -z "$AGENT" ]]; then
  echo "사용법: $0 <roulette-agent|vote-agent>" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/$AGENT"
ENV_FILE="$DIR/src/.env"
MANIFEST="$DIR/appPackage/manifest.json"
BUILD="$DIR/appPackage/build"

[[ -d "$DIR" ]]       || { echo "❌ 그런 에이전트가 없다: $DIR" >&2; exit 1; }
[[ -f "$ENV_FILE" ]]  || { echo "❌ $ENV_FILE 이 없다. src/.env.example 을 복사해서 채운다." >&2; exit 1; }
[[ -f "$MANIFEST" ]]  || { echo "❌ $MANIFEST 이 없다." >&2; exit 1; }

read_env () { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r'; }

BOT_ID="$(read_env BOT_ID || true)"
[[ -z "$BOT_ID" ]] && BOT_ID="$(read_env CLIENT_ID || true)"
TEAMS_APP_ID="$(read_env TEAMS_APP_ID || true)"
APP_NAME_SUFFIX="$(read_env APP_NAME_SUFFIX || true)"

if [[ -z "$BOT_ID" ]]; then
  echo "❌ $ENV_FILE 의 CLIENT_ID 가 비어 있다. STEP 1 에서 받은 Bot ID 를 넣는다." >&2
  exit 1
fi

# TEAMS_APP_ID 가 없으면 만들어서 .env 에 되써 넣는다 (다음 빌드에도 같은 값 유지).
if [[ -z "$TEAMS_APP_ID" ]]; then
  TEAMS_APP_ID="$(uuidgen | tr 'A-Z' 'a-z')"
  if grep -qE '^TEAMS_APP_ID=' "$ENV_FILE"; then
    sed -i '' "s|^TEAMS_APP_ID=.*|TEAMS_APP_ID=$TEAMS_APP_ID|" "$ENV_FILE"
  else
    printf '\nTEAMS_APP_ID=%s\n' "$TEAMS_APP_ID" >> "$ENV_FILE"
  fi
  echo "ℹ️  TEAMS_APP_ID 를 새로 발급해 $ENV_FILE 에 저장했다: $TEAMS_APP_ID"
fi

mkdir -p "$BUILD"

BOT_ID="$BOT_ID" TEAMS_APP_ID="$TEAMS_APP_ID" APP_NAME_SUFFIX="$APP_NAME_SUFFIX" \
python3 - "$MANIFEST" "$BUILD/manifest.json" <<'PY'
import json, os, sys

src, dst = sys.argv[1], sys.argv[2]
subs = {
    "${{BOT_ID}}": os.environ["BOT_ID"],
    "${{TEAMS_APP_ID}}": os.environ["TEAMS_APP_ID"],
    "${{APP_NAME_SUFFIX}}": os.environ.get("APP_NAME_SUFFIX", ""),
}

raw = open(src, encoding="utf-8").read()
for k, v in subs.items():
    raw = raw.replace(k, v)

if "${{" in raw:
    leftover = {t.split("}}")[0] + "}}" for t in raw.split("${{")[1:]}
    sys.exit(f"❌ 치환되지 않은 플레이스홀더: {sorted(leftover)}")

m = json.loads(raw)

# 치환 후 실제 값으로 Teams 매니페스트 제약을 검사한다.
limits = [
    ("name.short", m["name"]["short"], 30),
    ("name.full", m["name"]["full"], 100),
    ("description.short", m["description"]["short"], 80),
    ("description.full", m["description"]["full"], 4000),
]
for cl in m["bots"][0].get("commandLists", []):
    for c in cl["commands"]:
        limits += [("command.title", c["title"], 32),
                   ("command.description", c["description"], 128)]
for name, val, limit in limits:
    if len(val) > limit:
        sys.exit(f"❌ {name} 가 {len(val)}자로 한도 {limit}자를 넘었다: {val}")

with open(dst, "w", encoding="utf-8") as f:
    json.dump(m, f, ensure_ascii=False, indent=2)

print(f"   @멘션 이름 : @{m['name']['short']}")
print(f"   botId      : {m['bots'][0]['botId']}")
print(f"   앱 id      : {m['id']}")
PY

# manifest.json 과 아이콘이 zip 의 최상위에 있어야 한다. 하위 폴더로 들어가면 Teams 가 거부한다.
cp "$DIR/appPackage/color.png" "$DIR/appPackage/outline.png" "$BUILD/"
(cd "$BUILD" && rm -f appPackage.zip && zip -q appPackage.zip manifest.json color.png outline.png)

echo "✅ $BUILD/appPackage.zip"
(cd "$BUILD" && unzip -l appPackage.zip | sed -n '3,6p')
