"""Embeddings for the knowledge base: Voyage (Anthropic's recommended
embeddings partner) behind a two-method interface, batched and cached.

Voyage embeds a document and a query differently (`input_type`): a question
("can you put me to sleep?") and the passage that answers it ("oral
conscious sedation...") don't look alike, and the asymmetric modes are
trained to land them close anyway. So documents and queries go through
separate methods and must never be mixed.

DIM is part of the schema: chunks.embedding is vector(1024). Changing model
or dimension means a migration and a re-ingest (the ingest hash includes the
model, so a re-run re-embeds everything).
"""

import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DIM = 1024
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
MAX_BATCH_TEXTS = 128     # Voyage allows 1,000; smaller batches keep one failure cheap
# Voyage allows 1M tokens per request, but an account without a payment
# method is limited to 10K tokens and 3 requests *per minute*: a 20K-token
# batch could never succeed there. 8K fits every tier and costs a paid
# account nothing (our 60 chunks are 3 requests).
MAX_BATCH_TOKENS = 8_000
RATE_LIMIT_WAIT_SECS = 60.0  # Voyage's limits are per minute and it sends no Retry-After


class EmbeddingError(Exception):
    """Embedding failed: no key, provider down, or a bad response. Search
    callers degrade to keyword-only instead of failing the call."""


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


def estimate_tokens(text: str) -> int:
    """~4 characters per token for English. Close enough to size chunks and
    batches; Voyage reports the exact count in `usage`."""
    return max(1, len(text) // 4)


def _batches(texts: Sequence[str]) -> list[list[int]]:
    """Indices grouped so no batch exceeds the text or token limit."""
    batches, current, tokens = [], [], 0
    for i, text in enumerate(texts):
        t = estimate_tokens(text)
        if current and (len(current) >= MAX_BATCH_TEXTS or tokens + t > MAX_BATCH_TOKENS):
            batches.append(current)
            current, tokens = [], 0
        current.append(i)
        tokens += t
    if current:
        batches.append(current)
    return batches


class VoyageEmbedder:
    """Voyage over plain httpx: one endpoint, no SDK needed.

    Cache: an in-process LRU keyed by (model, input_type, sha256(text)).
    Queries repeat a lot on a phone line ("what are your hours?"), and a cache
    hit saves the ~100-200 ms round trip in the middle of a turn. Ingest has
    its own, persistent cache: chunks whose text hasn't changed keep their
    stored embedding (scripts/ingest.py).
    """

    def __init__(self, api_key: str | None, model: str, *, http: httpx.Client | None = None,
                 timeout: float = 10.0, query_timeout: float = 2.0, cache_size: int = 2048,
                 max_retries: int = 3):
        self.model = model
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=timeout)
        self._cache: OrderedDict[tuple[str, str, str], list[float]] = OrderedDict()
        self._cache_size = cache_size
        self._max_retries = max_retries
        self._timeout, self._query_timeout = timeout, query_timeout
        self.calls = 0  # API requests made; tests and the ingest summary read it

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, "document", retries=self._max_retries, timeout=self._timeout)

    def embed_query(self, text: str) -> list[float]:
        # No retries: a query runs mid-call, and keyword-only search now beats
        # a better search seconds from now (app/rag/search.py degrades).
        return self._embed([text], "query", retries=0, timeout=self._query_timeout)[0]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Many queries in one batched, retried request, for offline use (the
        eval). Results land in the cache, so later embed_query calls for the
        same text are free."""
        return self._embed(texts, "query", retries=self._max_retries, timeout=self._timeout)

    def _key(self, text: str, input_type: str) -> tuple[str, str, str]:
        return self.model, input_type, hashlib.sha256(text.encode()).hexdigest()

    def _embed(self, texts: Sequence[str], input_type: str, *, retries: int,
               timeout: float) -> list[list[float]]:
        out: list[list[float] | None] = [None] * len(texts)
        missing = []
        for i, text in enumerate(texts):
            hit = self._cache.get(self._key(text, input_type))
            if hit is not None:
                self._cache.move_to_end(self._key(text, input_type))
                out[i] = hit
            else:
                missing.append(i)
        todo = [texts[i] for i in missing]
        for batch in _batches(todo):
            vectors = self._request([todo[j] for j in batch], input_type, retries, timeout)
            for j, vector in zip(batch, vectors):
                i = missing[j]
                out[i] = vector
                self._remember(self._key(texts[i], input_type), vector)
        return out  # type: ignore[return-value]

    def _remember(self, key, vector) -> None:
        self._cache[key] = vector
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _request(self, texts: list[str], input_type: str, retries: int,
                 timeout: float) -> list[list[float]]:
        if not self._api_key:
            raise EmbeddingError("VOYAGE_API_KEY is not set")
        body = {"input": texts, "model": self.model, "input_type": input_type, "output_dimension": DIM}
        failure, delay = "", 0.0
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(delay)
            delay = 2.0 ** attempt  # next wait, unless the server says otherwise below
            self.calls += 1
            try:
                response = self._http.post(VOYAGE_URL, json=body, timeout=timeout,
                                           headers={"Authorization": f"Bearer {self._api_key}"})
            except httpx.HTTPError as e:
                failure = f"unreachable ({type(e).__name__})"
                logger.warning("Voyage %s, attempt %d", failure, attempt + 1)
                continue
            if response.status_code == 200:
                data = sorted(response.json()["data"], key=lambda d: d["index"])
                vectors = [d["embedding"] for d in data]
                if len(vectors) != len(texts) or any(len(v) != DIM for v in vectors):
                    raise EmbeddingError("Voyage returned the wrong number or size of vectors")
                return vectors
            # 429 (rate limit) and 5xx are worth retrying; other 4xx (bad key, bad request) aren't.
            if response.status_code != 429 and response.status_code < 500:
                raise EmbeddingError(f"Voyage HTTP {response.status_code}: {response.text[:200]}")
            failure = f"HTTP {response.status_code}"
            retry_after = response.headers.get("retry-after", "")
            if retry_after.replace(".", "", 1).isdigit():
                delay = float(retry_after)
            elif response.status_code == 429:
                delay = RATE_LIMIT_WAIT_SECS  # 1-2-4 s backoff can't outwait a per-minute window
            logger.warning("Voyage %s, attempt %d", failure, attempt + 1)
        raise EmbeddingError(f"Voyage {failure} after {retries + 1} attempt(s)")


_embedder: Embedder | None = None


def embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = VoyageEmbedder(settings.voyage_api_key, settings.embed_model,
                                   timeout=settings.embed_timeout_secs,
                                   query_timeout=settings.embed_query_timeout_secs)
    return _embedder


def set_embedder(value: Embedder | None) -> None:
    """Tests install a fake; None resets to the configured Voyage client."""
    global _embedder
    _embedder = value
