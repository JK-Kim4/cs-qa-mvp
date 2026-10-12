#!/usr/bin/env bash
# Hugging Face 공개 CS 데이터셋의 parquet 파일을 data/raw/<slug>/ 아래로 받는다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW="$ROOT/data/raw"

DATASETS=(
  "bitext/Bitext-customer-support-llm-chatbot-training-dataset"
  "Tobi-Bueck/customer-support-tickets"
  "TNE-AI/customer-support-on-twitter-conversation"
  "stindardlogic/customer-service-sft-50k"
  "Lakshan2003/customer_service_client_agent_conversations_40k_multi_task"
  "rjac/e-commerce-customer-support-qa"
)

for ds in "${DATASETS[@]}"; do
  slug="${ds//\//__}"
  mkdir -p "$RAW/$slug"
  curl -sf "https://datasets-server.huggingface.co/parquet?dataset=$ds" \
    | python3 -c 'import json,sys; [print(f["config"], f["split"], f["filename"], f["url"]) for f in json.load(sys.stdin)["parquet_files"]]' \
    | while read -r config split fname url; do
        out="$RAW/$slug/${config}__${split}__${fname}"
        [ -s "$out" ] && continue
        curl -sfL --retry 3 -o "$out" "$url"
        echo "ok $slug/${config}__${split}__${fname} $(du -h "$out" | cut -f1)"
      done
done
