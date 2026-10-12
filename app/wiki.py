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

    def search(self, query, k):
        q = tokenize(query)
        scored = sorted(zip(self.bm25.scores(q), self.pages), key=lambda x: -x[0])
        return q, [(p, s) for s, p in scored[:k] if s > 0]
