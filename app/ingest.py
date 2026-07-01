"""RAG ingestion for PayPilot.

Turns the dunning playbook (``data/playbook.md``) into a retrievable
knowledge base. The ``retrieve_context`` node queries this retriever so the
LLM nodes (diagnosis + message drafting) are grounded in real dunning
best-practice instead of hallucinating policy.

Two public helpers:

* ``load_playbook()`` - read the raw markdown source.
* ``get_retriever()`` - build an in-memory FAISS vector store and return a
  ``k=3`` retriever. Built once and cached (lazy singleton) so the embeddings /
  index cost is paid a single time per process. FAISS keeps the image light and
  the cold start fast (no heavy vector DB server to boot).

Tests monkeypatch the retriever, so none of this runs (and no OpenAI key /
network is required) under pytest.
"""

from __future__ import annotations

import os
import re

from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings

# --- Paths -----------------------------------------------------------------
# Resolve everything relative to the repo root (parent of this app/ package)
# so it works regardless of the caller's current working directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_APP_DIR)
PLAYBOOK_PATH = os.path.join(_ROOT_DIR, "data", "playbook.md")

# Lazy singleton: the built retriever is cached here after first use.
_retriever = None


def load_playbook() -> str:
    """Return the raw dunning playbook markdown (the RAG knowledge source)."""
    with open(PLAYBOOK_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _chunk_playbook() -> list[str]:
    """Split the playbook into retrieval chunks (pure, no key/network)."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
        separators=["\n## ", "\n### ", "\n\n", "\n", " ", ""],
    )
    return splitter.split_text(load_playbook())


class _Doc:
    """Minimal LangChain-Document stand-in: exposes ``page_content``."""

    def __init__(self, content: str) -> None:
        self.page_content = content


class _KeywordRetriever:
    """Offline retriever used when no OpenAI key is set.

    Scores playbook chunks by word overlap with the query and returns the top
    ``k``. It's genuine retrieval over the same knowledge source as the Chroma
    path - just lexical instead of embedding-based - so the demo's RAG step is
    real and runs with no key, no network, and no cost.
    """

    def __init__(self, k: int = 3) -> None:
        self._chunks = _chunk_playbook()
        self._k = k

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z_]+", text.lower()))

    def invoke(self, query: str):
        q = self._tokens(query)
        scored = sorted(
            self._chunks,
            key=lambda c: len(q & self._tokens(c)),
            reverse=True,
        )
        return [_Doc(c) for c in scored[: self._k]]


def _build_retriever():
    """Chunk the playbook, embed it, and build an in-memory FAISS index.

    Markdown headings keep each failure-reason section coherent, so we split on
    structural boundaries first and fall back to paragraphs/lines. ``k=3``
    returns the few most relevant snippets for each query. The index lives in
    memory for the process lifetime - the playbook is small and rebuilding on
    boot is cheaper than shipping/persisting a store.
    """
    chunks = _chunk_playbook()

    # OpenAIEmbeddings reads OPENAI_API_KEY from the environment.
    embeddings = OpenAIEmbeddings()

    vectorstore = FAISS.from_texts(texts=chunks, embedding=embeddings)

    return vectorstore.as_retriever(search_kwargs={"k": 3})


def get_retriever():
    """Return the cached playbook retriever, building it on first call.

    Lazy singleton: subsequent calls reuse the same in-process retriever so the
    embedding + index build happens only once.
    """
    global _retriever
    if _retriever is None:
        # No key -> lexical retriever (offline demo); key -> embedded FAISS index.
        if os.getenv("OPENAI_API_KEY"):
            _retriever = _build_retriever()
        else:
            _retriever = _KeywordRetriever(k=3)
    return _retriever
