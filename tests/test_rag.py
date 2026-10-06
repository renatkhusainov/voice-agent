"""Knowledge base (app/rag/, scripts/ingest.py, POST /rag/search).

Chunking, the Voyage client and RRF are tested offline. Search and ingest
need real pgvector: they run against a throwaway database `fde_rag_test` on
the local Postgres (never the dev DB), built by the real migrations, and
skip if that server isn't up. Embeddings come from a deterministic fake, so
nothing here calls Voyage.
"""

import hashlib
import itertools
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.rag import chunk as chunker
from app.rag import embed as embed_mod
from app.rag.embed import DIM, EmbeddingError, VoyageEmbedder
from app.rag.search import Hit, rrf, search

ROOT = Path(__file__).resolve().parents[1]


# ── chunking ─────────────────────────────────────────────────────────────────
DOC = """# Cancellation Policy

Intro line about cancellations.

## How much notice

Please give 24 hours' notice.

## Late fees

A $50 fee applies after the first missed appointment.
"""


def test_chunks_carry_the_doc_title_and_keep_headings():
    [only] = chunker.chunk_markdown(DOC)

    assert only.text.startswith("Cancellation Policy\n\n")
    assert "## Late fees" in only.text and "$50" in only.text
    assert only.ordinal == 0 and only.tokens == embed_mod.estimate_tokens(only.text)


def test_sections_are_packed_and_split_on_headings_under_the_budget():
    doc = "# T\n\n" + "\n\n".join(f"## Section {i}\n\n" + ("word " * 120) for i in range(6))

    chunks = chunker.chunk_markdown(doc, max_tokens=400)

    assert len(chunks) > 1 and all(c.tokens <= 400 for c in chunks)
    for c in chunks:  # every chunk starts at a heading, never mid-section
        assert c.text.split("\n\n", 2)[1].startswith("## Section")
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_packing_is_balanced_not_greedy():
    doc = "# T\n\n" + "\n\n".join(f"## S{i}\n\n" + ("word " * 100) for i in range(5))

    sizes = [c.tokens for c in chunker.chunk_markdown(doc, max_tokens=500)]

    assert len(sizes) == 2 and min(sizes) > 0.5 * max(sizes)  # not 480 + 60


def test_a_long_section_is_split_with_overlap():
    sentences = [f"Sentence number {i} says something specific about topic {i}." for i in range(80)]
    doc = "# T\n\n## Big\n\n" + " ".join(sentences)

    chunks = chunker.chunk_markdown(doc, max_tokens=200, overlap=40)

    assert len(chunks) >= 3 and all(c.tokens <= 200 for c in chunks)
    for a, b in itertools.pairwise(chunks):
        last_of_a = re.findall(r"Sentence number \d+", a.text)[-1]
        assert last_of_a in b.text  # the cut is covered from both sides


def test_a_doc_without_headings_is_one_chunk():
    [only] = chunker.chunk_markdown("Just a paragraph.\n\nAnd another.")

    assert only.text == "Just a paragraph.\n\nAnd another."


# ── Voyage client (httpx.MockTransport, no network) ──────────────────────────
def _voyage(handler, **kwargs):
    return VoyageEmbedder("test-key", "voyage-4-lite", http=httpx.Client(transport=httpx.MockTransport(handler)),
                          **kwargs)


def _ok(request):
    body = request.read().decode()
    import json
    payload = json.loads(body)
    return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)] * DIM}
                                              for i in range(len(payload["input"]))],
                                     "usage": {"total_tokens": 1}})


def test_documents_and_queries_use_their_own_input_type():
    seen = []

    def handler(request):
        import json
        seen.append(json.loads(request.read())["input_type"])
        return _ok(request)

    emb = _voyage(handler)
    emb.embed_documents(["a"])
    emb.embed_query("b")

    assert seen == ["document", "query"]


def test_batches_and_preserves_order(monkeypatch):
    monkeypatch.setattr(embed_mod, "MAX_BATCH_TEXTS", 2)
    emb = _voyage(_ok)

    vectors = emb.embed_documents(["a", "b", "c", "d", "e"])

    assert emb.calls == 3
    assert [v[0] for v in vectors] == [0.0, 1.0, 0.0, 1.0, 0.0]  # index within each batch


def test_batched_queries_warm_the_cache_for_single_queries():
    emb = _voyage(_ok)

    emb.embed_queries(["hours?", "parking?", "insurance?"])
    emb.embed_query("parking?")

    assert emb.calls == 1


def test_cache_skips_the_api_for_repeated_text():
    emb = _voyage(_ok)

    emb.embed_query("what are your hours?")
    emb.embed_query("what are your hours?")
    emb.embed_documents(["what are your hours?"])  # same text, other input_type: not a hit

    assert emb.calls == 2


def test_rate_limit_is_retried_for_documents(monkeypatch):
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: sleeps.append(s))
    sleeps, responses = [], iter([httpx.Response(429, headers={"retry-after": "3"}), None])

    def handler(request):
        r = next(responses)
        return r if r is not None else _ok(request)

    assert len(_voyage(handler).embed_documents(["x"])) == 1
    assert sleeps == [3.0]


def test_a_rate_limit_without_retry_after_waits_out_the_minute(monkeypatch):
    monkeypatch.setattr(embed_mod.time, "sleep", lambda s: sleeps.append(s))
    sleeps, responses = [], iter([httpx.Response(429), None])

    def handler(request):
        r = next(responses)
        return r if r is not None else _ok(request)

    _voyage(handler).embed_documents(["x"])

    assert sleeps == [embed_mod.RATE_LIMIT_WAIT_SECS]


def test_batches_stay_under_the_token_budget():
    texts = ["word " * 4000] * 3  # ~5K tokens each by the 4-chars estimate

    assert [len(b) for b in embed_mod._batches(texts)] == [1, 1, 1]


def test_a_query_fails_fast_without_retrying():
    emb = _voyage(lambda request: httpx.Response(503))

    with pytest.raises(EmbeddingError):
        emb.embed_query("hours?")
    assert emb.calls == 1


def test_a_bad_key_is_not_retried():
    emb = _voyage(lambda request: httpx.Response(401, json={"detail": "bad key"}))

    with pytest.raises(EmbeddingError, match="401"):
        emb.embed_documents(["x"])
    assert emb.calls == 1


def test_no_key_raises_without_a_request():
    emb = VoyageEmbedder(None, "voyage-4-lite", http=httpx.Client(transport=httpx.MockTransport(_ok)))

    with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
        emb.embed_query("x")
    assert emb.calls == 0


# ── RRF ──────────────────────────────────────────────────────────────────────
def _hit(chunk_id, **ranks):
    return Hit(chunk_id, 1, "t", "s.md", 0, "text", **ranks)


def test_rrf_rewards_agreement_between_lists():
    vector = [_hit(1, vector_rank=1), _hit(2, vector_rank=2), _hit(3, vector_rank=3)]
    keyword = [_hit(3, keyword_rank=1), _hit(4, keyword_rank=2)]

    fused = rrf(vector, keyword)

    assert fused[0].chunk_id == 3  # 3rd + 1st beats 1st alone
    assert (fused[0].vector_rank, fused[0].keyword_rank) == (3, 1)
    assert fused[0].score == pytest.approx(1 / 63 + 1 / 61)
    assert [h.chunk_id for h in fused] == [3, 1, 2, 4]


# ── Postgres + pgvector: search, ingest, endpoint ────────────────────────────
class FakeEmbedder:
    """Bag of words hashed into DIM buckets, L2-normalized: chunks sharing
    words with the query are close. Deterministic, offline."""

    model = "fake-bow"

    def __init__(self):
        self.calls = 0
        self.embedded = 0

    def _vec(self, text_: str) -> list[float]:
        v = [0.0] * DIM
        for word in re.findall(r"[a-z0-9$]+", text_.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts):
        self.calls += 1
        self.embedded += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text_):
        return self._vec(text_)

    def embed_queries(self, texts):
        return [self._vec(t) for t in texts]


class BrokenEmbedder(FakeEmbedder):
    def embed_query(self, text_):
        raise EmbeddingError("Voyage HTTP 503")


def _pg_url(database: str) -> str | None:
    if not (settings.postgres_user and settings.postgres_password):
        return None
    return (make_url("postgresql+psycopg://localhost:5432")
            .set(username=settings.postgres_user, password=settings.postgres_password, database=database)
            .render_as_string(hide_password=False))


@pytest.fixture(scope="module")
def pg():
    url = _pg_url("fde_rag_test")
    if url is None:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set")
    try:
        admin = create_engine(_pg_url("postgres"), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 2})
        with admin.connect() as c:
            if not c.execute(text("SELECT 1 FROM pg_available_extensions WHERE name='vector'")).scalar():
                pytest.skip("Postgres has no pgvector: docker compose up -d postgres")
            c.execute(text("DROP DATABASE IF EXISTS fde_rag_test WITH (FORCE)"))
            c.execute(text("CREATE DATABASE fde_rag_test"))
    except Exception as e:  # noqa: BLE001 - any connection problem means "not available here"
        pytest.skip(f"Postgres not reachable: {type(e).__name__}")
    # The real migrations, not create_all: the tsv column and indexes live there.
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True,
                   env={**os.environ, "DATABASE_URL": url}, capture_output=True)
    engine = create_engine(url)
    yield sessionmaker(bind=engine)
    engine.dispose()
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS fde_rag_test WITH (FORCE)"))


@pytest.fixture
def db(pg):
    from app.models.models import Practice
    session = pg()
    session.execute(text("TRUNCATE practices, documents, chunks RESTART IDENTITY CASCADE"))
    session.add_all([Practice(id=1, name="Sunshine Dental", phone="+15550009999", timezone="America/New_York"),
                     Practice(id=2, name="Other Dental", phone="+15550008888", timezone="America/New_York")])
    session.commit()
    yield session
    session.close()


def _write_kb(path: Path, docs: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, body in docs.items():
        (path / name).write_text(body)
    return path


KB = {
    "insurance.md": "# Insurance\n\n## Plans we accept\n\nWe are in network with Delta Dental PPO and Cigna Dental PPO.\n",
    "gum.md": "# Gum disease\n\n## Deep cleaning\n\nScaling and root planing (D4341) is $225-$285 per quadrant.\n",
    "parking.md": "# Parking\n\n## Garage\n\nFree garage parking behind the building, entrance on S Albany Ave.\n",
    "_FACTS.md": "# Fact sheet\n\nNot ingested.\n",
}


@pytest.fixture
def kb(tmp_path):
    return _write_kb(tmp_path / "kb", KB)


def test_ingest_creates_documents_and_chunks_and_skips_underscore_files(db, kb):
    from scripts.ingest import ingest
    emb = FakeEmbedder()

    report = ingest(db, 1, kb, emb=emb)

    assert sorted(report.created) == ["gum.md", "insurance.md", "parking.md"]
    assert emb.calls == 1  # all docs' chunks in one batched call, not one per doc
    assert db.execute(text("SELECT count(*) FROM chunks")).scalar() == 3
    assert db.execute(text("SELECT count(*) FROM chunks WHERE tsv @@ to_tsquery('english', 'albany')")).scalar() == 1


def test_ingest_is_idempotent_and_reembeds_only_what_changed(db, kb):
    from scripts.ingest import ingest
    emb = FakeEmbedder()
    ingest(db, 1, kb, emb=emb)

    again = ingest(db, 1, kb, emb=emb)
    assert (again.chunks_embedded, len(again.unchanged)) == (0, 3)

    (kb / "parking.md").write_text(KB["parking.md"] + "\n## Bus\n\nHART stop at Kennedy & Albany.\n")
    edited = ingest(db, 1, kb, emb=emb)
    assert edited.updated == ["parking.md"] and edited.chunks_embedded == 1
    assert db.execute(text("SELECT count(*) FROM chunks")).scalar() == 3


def test_prune_removes_documents_whose_file_is_gone(db, kb):
    from scripts.ingest import ingest
    ingest(db, 1, kb, emb=FakeEmbedder())
    (kb / "gum.md").unlink()

    report = ingest(db, 1, kb, emb=FakeEmbedder(), prune=True)

    assert report.deleted == ["gum.md"]
    assert db.execute(text("SELECT count(*) FROM chunks")).scalar() == 2


def test_search_modes_rank_the_right_chunk_first(db, kb):
    from scripts.ingest import ingest
    emb = FakeEmbedder()
    ingest(db, 1, kb, emb=emb)

    for mode in ("vector", "keyword", "hybrid"):
        hits = search(db, 1, "do you take delta dental", k=3, mode=mode, emb=emb).hits
        assert hits[0].source == "insurance.md", mode
    hybrid = search(db, 1, "where is the parking garage", k=3, emb=emb).hits[0]
    assert hybrid.source == "parking.md" and hybrid.vector_rank and hybrid.keyword_rank


def test_keyword_search_finds_an_exact_code(db, kb):
    from scripts.ingest import ingest
    ingest(db, 1, kb, emb=FakeEmbedder())

    [hit] = search(db, 1, "D4341", k=3, mode="keyword").hits

    assert hit.source == "gum.md" and hit.keyword_score > 0


def test_search_never_crosses_practices(db, kb, tmp_path):
    from scripts.ingest import ingest
    emb = FakeEmbedder()
    ingest(db, 1, kb, emb=emb)
    other = _write_kb(tmp_path / "other", {"insurance.md": "# Insurance\n\nWe take Delta Dental only on Mondays.\n"})
    ingest(db, 2, other, emb=emb)

    for mode in ("vector", "keyword", "hybrid"):
        hits = search(db, 2, "delta dental insurance", k=10, mode=mode, emb=emb).hits
        assert hits and {h.document_id for h in hits} == {
            db.execute(text("SELECT id FROM documents WHERE practice_id = 2")).scalar()}, mode


def test_hybrid_degrades_to_keyword_when_embedding_fails(db, kb):
    from scripts.ingest import ingest
    ingest(db, 1, kb, emb=FakeEmbedder())

    result = search(db, 1, "delta dental", k=3, emb=BrokenEmbedder())

    assert result.degraded and "keyword-only" in result.degraded
    assert result.hits[0].source == "insurance.md"
    with pytest.raises(EmbeddingError):
        search(db, 1, "delta dental", k=3, mode="vector", emb=BrokenEmbedder())


def test_search_endpoint_returns_ranked_chunks_with_scores(db, kb, pg):
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app
    from scripts.ingest import ingest
    emb = FakeEmbedder()
    ingest(db, 1, kb, emb=emb)
    embed_mod.set_embedder(emb)

    def pg_db():
        session = pg()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = pg_db
    try:
        with TestClient(app) as client:
            r = client.post("/rag/search", json={"practice_id": 1, "query": "deep cleaning price", "k": 2})
            missing = client.post("/rag/search", json={"practice_id": 99, "query": "x"})
    finally:
        app.dependency_overrides.clear()
        embed_mod.set_embedder(None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "hybrid" and body["degraded"] is None
    assert [h["rank"] for h in body["results"]] == [1, 2]
    top = body["results"][0]
    assert top["source"] == "gum.md" and top["score"] > 0 and "D4341" in top["text"]
    assert body["results"][0]["score"] >= body["results"][1]["score"]
    assert missing.status_code == 404
