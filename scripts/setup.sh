#!/usr/bin/env bash
# 프로젝트 셋업(make setup) 후 OpenAI / OpenRouter / 노션 키를 입력받아 .env 에 저장한다.
# 키 입력은 화면에 표시되지 않는다. Enter 만 누르면 기존 값을 그대로 둔다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"

cd "$ROOT"
make setup

if [ ! -f "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  echo ".env 를 .env.example 에서 만들었습니다."
fi
chmod 600 "$ENV_FILE"

current() {
  sed -n "s/^$1=//p" "$ENV_FILE" | head -1
}

# KEY=값 줄을 바꾼다. 줄이 없으면 끝에 추가한다. 값은 ENVIRON 으로 넘겨 특수문자를 해석하지 않는다.
set_key() {
  local tmp="$ENV_FILE.tmp"
  (umask 077; : > "$tmp")
  KEY="$1" VAL="$2" awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"] }
    index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

ask_key() {
  local name="$1" desc="$2" url="$3" cur input
  cur="$(current "$name")"
  echo
  echo "$name — $desc"
  echo "  발급: $url"
  if [ -n "$cur" ]; then
    echo "  현재: 설정됨 (${cur:0:6}…${cur: -4})"
  else
    echo "  현재: 미설정"
  fi
  read -rsp "  새 값 (Enter: 유지/건너뛰기): " input
  echo
  if [ -n "$input" ]; then
    set_key "$name" "$input"
    echo "  저장했습니다."
  fi
}

ask_key OPENAI_API_KEY "답변 생성·검색어 변환(LLM)" "https://platform.openai.com/api-keys"
ask_key OPENROUTER_API_KEY "판단 모델(TypeSafe Jev via OpenRouter)" "https://openrouter.ai/keys"
ask_key NOTION_TOKEN "노션 티켓 생성(상담 종료)" "노션의 통합(연결) 설정 화면. 통합을 CS 처리 보드 페이지에 연결해야 합니다"
ask_key NOTION_DATA_SOURCE_ID "티켓을 만들 CS 처리 보드의 데이터 소스 ID" "보드 관리자에게 확인"

echo
echo "완료. 'make run' 으로 서버를 실행하세요. 키를 바꿨다면 서버를 재시작해야 합니다."
