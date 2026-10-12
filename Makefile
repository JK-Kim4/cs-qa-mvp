# 3.10 이상 중 가장 최신 파이썬을 고른다. macOS 기본 python3 는 3.9 라 쓸 수 없다.
PY ?= $(shell for v in 3.14 3.13 3.12 3.11 3.10; do command -v python$$v && break; done)
VENV := .venv
BIN := $(VENV)/bin
PORT ?= 8000

.PHONY: setup run test data wiki

setup:
	@test -n "$(PY)" || (echo "Python 3.10 이상이 필요합니다. 예: brew install python@3.13" && exit 1)
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -r requirements.txt

run:
	$(BIN)/uvicorn app.main:app --reload --port $(PORT)

test:
	$(BIN)/pytest -q

# 원본 데이터 다운로드 + 정규화 (약 400MB 다운로드, 1.1GB 생성)
data:
	scripts/download.sh
	$(BIN)/python scripts/normalize.py

wiki:
	$(BIN)/python scripts/build_wiki.py
