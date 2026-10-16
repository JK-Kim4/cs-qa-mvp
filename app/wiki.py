"""위키 페이지 로딩과 BM25 키워드 검색."""
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# 검색에 의미 없는 영어 기능어. 위키가 영어라 영어만 다룬다.
STOPWORDS = set(
    "a an the and or but if to of in on at for with from by as is are was were be been am "
    "i me my we our you your he she it its they them their this that these those "
    "do does did can could would should will shall may might must have has had "
    "what how why when where which who whom please hi hello thanks thank there here "
    "not no so just about into than then too very also".split()
)
# bitext 원본의 {{Order Number}} 같은 자리표시자
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


def tokenize(text):
    text = PLACEHOLDER.sub(" ", text.lower())
    return [t for t in re.findall(r"[a-z0-9]+", text) if t not in STOPWORDS and len(t) > 1]


@dataclass
class Page:
    id: str
    path: str
    title: str
    source: str
    group: str
    topic: str
    records: int
    examples: list
    search_text: str
    license: str = ""

    def body(self, wiki_dir: Path):
        text = (wiki_dir / self.path).read_text(encoding="utf-8")
        # 첫 줄 메타데이터(--- json ---)는 떼고 본문만 돌려준다.
        return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / max(len(docs), 1)
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query_tokens):
        out = []
        for tf, length in zip(self.tfs, self.lens):
            s = 0.0
            for t in query_tokens:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            out.append(s)
        return out


# 가이드 화면에 보여 줄 데이터셋·그룹 설명. 위키 원문은 영어라 한국어 이름을 붙인다.
DATASETS = {
    "bitext": ("일반 CS", "주문·환불·계정 등 일반 고객센터 문의 유형"),
    "ecommerce": ("이커머스", "배송·보증·반품 등 이커머스 FAQ"),
    "sft50k": ("업종별", "은행·SaaS·여행 등 업종별 고객 이슈"),
}
GROUP_KO = {
    "ACCOUNT": "계정", "CANCEL": "취소 수수료", "CONTACT": "상담 연결", "DELIVERY": "배송 기간", "FEEDBACK": "불만·리뷰",
    "INVOICE": "청구서", "ORDER": "주문", "PAYMENT": "결제", "REFUND": "환불", "SHIPPING": "배송지", "SUBSCRIPTION": "구독",
    "Cancellations and returns": "취소·반품", "Login and Account": "로그인·계정", "Order": "주문", "Shipping": "배송",
    "Shopping": "쇼핑", "Warranty": "보증",
    "banking": "은행", "ecommerce": "이커머스", "food_delivery": "음식 배달", "healthcare": "의료", "hr_internal": "사내 인사",
    "saas": "SaaS", "telecom": "통신", "travel": "여행",
}


def readable(text):
    """{{Order Number}} 같은 자리표시자를 [Order Number] 로 바꿔 사람이 읽기 쉽게 한다."""
    return PLACEHOLDER.sub(lambda m: f"[{m.group(0)[2:-2].strip()}]", text)


class Wiki:
    def __init__(self, wiki_dir: Path):
        self.dir = wiki_dir
        index_file = wiki_dir / "_index.jsonl"
        if not index_file.exists():
            raise FileNotFoundError(f"{index_file} 가 없습니다. scripts/build_wiki.py 를 먼저 실행하세요.")
        with open(index_file, encoding="utf-8") as f:
            fields = Page.__dataclass_fields__
            self.pages = [Page(**{k: v for k, v in json.loads(l).items() if k in fields}) for l in f]
        self.by_id = {p.id: p for p in self.pages}
        self.bm25 = BM25([tokenize(p.search_text) for p in self.pages])

    def guide(self, excerpt_chars=320):
        """데이터셋 → 그룹 → 주제 구조. 주제마다 예시 질문과 대표 답변 앞부분을 담는다."""
        datasets = {}
        for p in self.pages:
            label, desc = DATASETS.get(p.source, (p.source, ""))
            ds = datasets.setdefault(p.source, {"id": p.source, "label": label, "desc": desc, "license": p.license,
                                                "pages": 0, "records": 0, "groups": {}})
            m = re.search(r"^## 대표 답변\n(.*?)(?=^## )", p.body(self.dir), flags=re.S | re.M)
            answer = " ".join(l for l in (m.group(1) if m else "").strip().splitlines() if not l.startswith(">")).strip()
            answer = readable(answer)
            group = ds["groups"].setdefault(p.group, {"name": p.group, "label": GROUP_KO.get(p.group, p.group), "topics": []})
            group["topics"].append({
                "id": p.id,
                "title": p.title.split(" · ", 1)[-1],
                "path": f"wiki/{p.path}",
                "records": p.records,
                "examples": [readable(e) for e in p.examples[:3]],
                "answer": answer[:excerpt_chars] + ("…" if len(answer) > excerpt_chars else ""),
            })
            ds["pages"] += 1
            ds["records"] += p.records
        out = [{**d, "groups": list(d["groups"].values())} for d in datasets.values()]
        return {
            "total": {"pages": len(self.pages), "records": sum(d["records"] for d in out), "datasets": len(out)},
            "datasets": out,
        }

    def search(self, query, k):
        q = tokenize(query)
        scored = sorted(zip(self.bm25.scores(q), self.pages), key=lambda x: -x[0])
        return q, [(p, s) for s, p in scored[:k] if s > 0]
