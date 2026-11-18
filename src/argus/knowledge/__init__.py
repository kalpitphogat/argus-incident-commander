"""Runbook knowledge base: Markdown runbooks with machine-readable action templates,
served through a LangChain retriever (BM25, no embeddings or network needed)."""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import yaml
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

RUNBOOK_DIR = Path(__file__).parent / "runbooks"
_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset("a an and are as at be by for from if in into is it its of on or the to with this that do not".split())


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower().replace("-", "_")) if t not in _STOP and len(t) > 1]


def _parse(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front) or {}
    meta["source"] = path.name
    return Document(page_content=body.strip(), metadata=meta, id=meta.get("id", path.stem))


@lru_cache(maxsize=1)
def load_runbooks() -> tuple[Document, ...]:
    return tuple(_parse(p) for p in sorted(RUNBOOK_DIR.glob("*.md")))


class RunbookRetriever(BaseRetriever):
    """Okapi BM25 over runbooks, boosted by category / component metadata."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    k: int = 3
    k1: float = 1.4
    b: float = 0.75
    docs: tuple[Document, ...] = Field(default_factory=load_runbooks)

    def _index(self):
        tokenized = [_tokens(d.metadata.get("title", "") + " " + " ".join(d.metadata.get("categories", [])) + " "
                             + d.page_content) for d in self.docs]
        df: Counter = Counter()
        for toks in tokenized:
            df.update(set(toks))
        avgdl = sum(len(t) for t in tokenized) / max(1, len(tokenized))
        return tokenized, df, avgdl

    def score(self, query: str) -> list[tuple[float, Document]]:
        tokenized, df, avgdl = self._index()
        q = _tokens(query)
        n = len(self.docs)
        scored = []
        for doc, toks in zip(self.docs, tokenized):
            tf = Counter(toks)
            s = 0.0
            for term in q:
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                s += idf * tf[term] * (self.k1 + 1) / (tf[term] + self.k1 * (1 - self.b + self.b * len(toks) / avgdl))
            cats = set(doc.metadata.get("categories", []))
            if cats & set(q):
                s *= 1.6
            scored.append((s, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        return [d for s, d in self.score(query)[: self.k] if s > 0]


def runbook_for_category(category: str) -> list[Document]:
    return [d for d in load_runbooks() if category in d.metadata.get("categories", [])]
