"""data/normalized 의 CS 기록을 주제별 마크다운 위키(wiki/)로 묶는다.

- 공유 가능한 라이선스(bitext, sft50k, ecommerce)만 쓴다. twitter/tickets 는 NC 라이선스라 제외.
- 주제 = 원본의 (카테고리, 세부유형) 조합. 페이지마다 대표 답변, 질문 예시, 답변 사례를 담는다.
- 검색용 텍스트는 wiki/_index.jsonl 에 따로 둔다 (페이지 본문보다 질문 예시를 훨씬 많이 담음).
"""
import json
import random
import re
import shutil
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NORM = ROOT / "data" / "normalized"
WIKI = ROOT / "wiki"

SEED = 42
EXAMPLE_QUESTIONS = 20
CASES = 3
SEARCH_QUESTIONS = 400
MEDOID_POOL = 150

LICENSES = {"bitext": "CDLA-Sharing-1.0", "sft50k": "Apache-2.0", "ecommerce": "MIT"}


def humanize(s):
    return re.sub(r"[_\-]+", " ", s or "").strip()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def group_key(rec):
    src = rec["source"]
    if src == "bitext":
        return (rec["category"], rec["intent"])
    if src == "sft50k":
        return (rec["domain"], rec["category"])
    return (rec["category"], rec["intent"])  # ecommerce: issue_area, issue_category


def title_of(src, a, b):
    if src == "bitext":
        return f"{humanize(a).title()} · {humanize(b)}"
    if src == "sft50k":
        return f"{humanize(a).title()} · {humanize(b)}"
    return f"{a} · {b}"


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def medoid(answers):
    """다른 답변들과 단어 겹침이 가장 큰 답변 = 그 주제의 가장 전형적인 답변."""
    pool = answers[:MEDOID_POOL]
    toks = [tokens(a) for a in pool]

    def centrality(i):
        return sum(len(toks[i] & t) / (len(toks[i] | t) or 1) for j, t in enumerate(toks) if j != i)

    return pool[max(range(len(pool)), key=centrality)]


def dedupe(items):
    seen, out = set(), []
    for x in items:
        k = re.sub(r"\s+", " ", x.strip().lower())
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out


def one_line(s, limit=300):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def render(page, recs, rng):
    questions = dedupe(r["question"] for r in recs)
    rng.shuffle(questions)
    answers = [r["answer"] for r in recs]
    rng.shuffle(answers)
    cases = rng.sample(recs, min(CASES, len(recs)))

    fm = {k: page[k] for k in ("id", "title", "source", "license", "group", "topic", "records")}
    lines = ["---", json.dumps(fm, ensure_ascii=False), "---", "", f"# {page['title']}", ""]
    lines += [
        "## 개요", "",
        f"- 출처: `{page['source']}` ({page['license']})",
        f"- 분류: {page['group']} / {page['topic']}",
        f"- 근거 기록 수: {page['records']:,}건", "",
        "## 대표 답변", "",
        "> 이 주제에서 가장 전형적인 상담원 답변 (다른 답변들과 내용이 가장 많이 겹치는 답변)", "",
        medoid(answers).strip(), "",
        "## 자주 들어오는 질문 예시", "",
    ]
    lines += [f"- {one_line(q)}" for q in questions[:EXAMPLE_QUESTIONS]]
    lines += ["", "## 답변 사례", ""]
    for i, c in enumerate(cases, 1):
        lines += [f"### 사례 {i}", "", f"**고객:** {c['question'].strip()}", "", f"**상담원:** {c['answer'].strip()}", ""]
    return "\n".join(lines), questions


def main():
    groups = defaultdict(list)
    for src in LICENSES:
        with open(NORM / f"{src}.jsonl", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                a, b = group_key(rec)
                if a and b:
                    groups[(src, a, b)].append(rec)

    if WIKI.exists():
        shutil.rmtree(WIKI)
    WIKI.mkdir()
    rng = random.Random(SEED)
    index = []
    for (src, a, b), recs in sorted(groups.items()):
        rel = Path(src) / slug(a) / f"{slug(b)}.md"
        page = {
            "id": f"{src}/{slug(a)}/{slug(b)}",
            "path": rel.as_posix(),
            "title": title_of(src, a, b),
            "source": src,
            "license": LICENSES[src],
            "group": a,
            "topic": b,
            "records": len(recs),
        }
        text, questions = render(page, recs, rng)
        (WIKI / rel).parent.mkdir(parents=True, exist_ok=True)
        (WIKI / rel).write_text(text, encoding="utf-8")
        page["examples"] = [one_line(q, 160) for q in questions[:3]]
        page["search_text"] = "\n".join(
            [page["title"], humanize(a), humanize(b)] + questions[:SEARCH_QUESTIONS]
        )
        index.append(page)

    with open(WIKI / "_index.jsonl", "w", encoding="utf-8") as f:
        for p in index:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    (WIKI / "README.md").write_text(
        "# wiki\n\n`scripts/build_wiki.py` 가 생성한 CS 위키. 직접 수정하지 말고 스크립트를 다시 돌린다.\n\n"
        "원본 데이터 라이선스: bitext(CDLA-Sharing-1.0), sft50k(Apache-2.0), ecommerce(MIT).\n"
        "bitext 에서 파생된 페이지는 CDLA-Sharing-1.0 조건으로 공유된다.\n",
        encoding="utf-8",
    )
    print(f"pages={len(index)} -> {WIKI}")


if __name__ == "__main__":
    main()
