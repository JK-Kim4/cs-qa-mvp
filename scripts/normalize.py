"""data/raw 의 데이터셋을 공통 스키마 JSONL(data/normalized/<source>.jsonl)로 변환한다.

레코드 스키마:
  id, source, license, synthetic, language, domain, category, intent,
  question  - 첫 상담원 응답 전까지의 고객 발화
  answer    - 그 직후 상담원 응답
  conversation - [{"role": "customer"|"agent", "text": ...}] 전체 대화
  meta      - 원본 고유 필드
"""
import json
import re
import sys
from pathlib import Path

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "normalized"


def rows(slug):
    for f in sorted((RAW / slug).glob("*.parquet")):
        pf = pq.ParquetFile(f)
        for batch in pf.iter_batches(batch_size=5000):
            yield from batch.to_pylist()


def first_exchange(conv):
    """첫 상담원 응답 직전까지의 고객 발화와, 그 직후 이어지는 상담원 발화를 묶어 반환."""
    q, a = [], []
    for turn in conv:
        if turn["role"] == "customer":
            if a:
                break
            q.append(turn["text"])
        elif q:
            a.append(turn["text"])
    return "\n".join(q).strip(), "\n".join(a).strip()


def record(source, idx, **kw):
    conv = kw.get("conversation") or []
    if "question" not in kw:
        kw["question"], kw["answer"] = first_exchange(conv)
    return {
        "id": f"{source}:{idx}",
        "source": source,
        "license": LICENSES[source],
        "synthetic": SYNTHETIC[source],
        "language": kw.get("language", "en"),
        "domain": kw.get("domain"),
        "category": kw.get("category"),
        "intent": kw.get("intent"),
        "question": kw["question"],
        "answer": kw["answer"],
        "conversation": conv,
        "meta": kw.get("meta", {}),
    }


# 라벨이 붙은 줄("Customer: ...")로 시작하는 대화 문자열을 턴 리스트로 쪼갠다.
# 라벨 없는 줄은 직전 턴의 이어지는 내용으로 본다.
def parse_labeled(text, labels):
    conv = []
    pattern = re.compile(r"^(%s):\s?(.*)$" % "|".join(labels))
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            conv.append({"role": labels[m.group(1)], "text": m.group(2)})
        elif conv and line.strip():
            conv[-1]["text"] += "\n" + line
    return conv


def bitext():
    for i, r in enumerate(rows("bitext__Bitext-customer-support-llm-chatbot-training-dataset")):
        yield record(
            "bitext", i,
            question=r["instruction"], answer=r["response"],
            conversation=[{"role": "customer", "text": r["instruction"]},
                          {"role": "agent", "text": r["response"]}],
            category=r["category"], intent=r["intent"],
            meta={"flags": r["flags"]},
        )


def tickets():
    for i, r in enumerate(rows("Tobi-Bueck__customer-support-tickets")):
        # 원본에 줄바꿈이 리터럴 "\\n" 으로 들어 있다.
        fix = lambda s: (s or "").replace("\\n", "\n").strip()
        q = (fix(r["subject"]) + "\n\n" + fix(r["body"])).strip()
        a = fix(r["answer"])
        tags = [r[f"tag_{n}"] for n in range(1, 9) if r.get(f"tag_{n}")]
        yield record(
            "tickets", i, question=q, answer=a,
            conversation=[{"role": "customer", "text": q}, {"role": "agent", "text": a}],
            language=r["language"], category=r["queue"], intent=r["type"],
            meta={"priority": r["priority"], "tags": tags, "version": r["version"]},
        )


def twitter():
    labels = {"Customer": "customer", "Support": "agent"}
    for r in rows("TNE-AI__customer-support-on-twitter-conversation"):
        conv = parse_labeled(r["conversation"], labels)
        yield record(
            "twitter", r["conversation_id"], conversation=conv, domain=r["company"],
            meta={"summary": r["summary"]} if r["summary"] else {},
        )


def sft50k():
    roles = {"human": "customer", "gpt": "agent"}
    for r in rows("stindardlogic__customer-service-sft-50k"):
        turns = r["conversations"]
        system = next((t["value"] for t in turns if t["from"] == "system"), None)
        conv = [{"role": roles[t["from"]], "text": t["value"]} for t in turns if t["from"] in roles]
        md = r["metadata"] or {}
        yield record(
            "sft50k", r["id"], conversation=conv,
            domain=md.get("industry"), category=md.get("issue_type"),
            meta={"system_prompt": system},
        )


def calls40k():
    roles = {"client": "customer", "agent": "agent"}
    for r in rows("Lakshan2003__customer_service_client_agent_conversations_40k_multi_task"):
        conv = [{"role": roles.get(t["speaker"], t["speaker"]), "text": t["text"]} for t in r["conversation"]]
        yield record(
            "calls40k", r["conversation_id"], conversation=conv,
            meta={"customer_sentiment": r["customer_sentiment"], "num_turns": r["num_turns"]},
        )


def ecommerce():
    labels = {"Customer": "customer", "Agent": "agent"}
    for i, r in enumerate(rows("rjac__e-commerce-customer-support-qa")):
        conv = parse_labeled(r["conversation"], labels)
        # qa 필드에 사람이 요약한 질문/해결책이 있으면 그걸 question/answer 로 쓴다.
        kw = {}
        try:
            k = json.loads(r["qa"])["knowledge"][0]
            kw = {"question": k["customer_summary_question"], "answer": k["agent_summary_solution"]}
        except (TypeError, ValueError, KeyError, IndexError):
            pass
        yield record(
            "ecommerce", i, conversation=conv, **kw,
            domain=r["product_category"], category=r["issue_area"], intent=r["issue_category"],
            meta={k: r[k] for k in ("issue_sub_category", "product_sub_category", "customer_sentiment",
                                    "issue_complexity", "agent_experience_level")},
        )


SOURCES = {
    "bitext": bitext, "tickets": tickets, "twitter": twitter,
    "sft50k": sft50k, "calls40k": calls40k, "ecommerce": ecommerce,
}
LICENSES = {
    "bitext": "CDLA-Sharing-1.0", "tickets": "CC-BY-NC-4.0", "twitter": "CC-BY-NC-SA-4.0 (TWCS)",
    "sft50k": "Apache-2.0", "calls40k": "unspecified", "ecommerce": "MIT",
}
# None = 원본에 명시되지 않음
SYNTHETIC = {
    "bitext": True, "tickets": True, "twitter": False,
    "sft50k": True, "calls40k": None, "ecommerce": True,
}


def main(names):
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names or SOURCES:
        kept = dropped = 0
        with open(OUT / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for rec in SOURCES[name]():
                # 질문-답변 쌍이 성립하지 않는 레코드는 버린다.
                if not rec["question"] or not rec["answer"]:
                    dropped += 1
                    continue
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                kept += 1
        size = (OUT / f"{name}.jsonl").stat().st_size / 1e6
        print(f"{name:10s} kept={kept:>8,} dropped={dropped:>6,} {size:8.1f}MB")


if __name__ == "__main__":
    main(sys.argv[1:])
