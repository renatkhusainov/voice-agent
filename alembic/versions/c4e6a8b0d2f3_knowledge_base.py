"""knowledge_base

pgvector knowledge base (app/rag/, docs/notes/rag.md): documents and
chunks, each chunk with a 1024-dim Voyage embedding (HNSW, cosine) and a
generated tsvector (GIN) for keyword search. Needs the pgvector image
(docker-compose.yaml: pgvector/pgvector:pg16-trixie).

Revision ID: c4e6a8b0d2f3
Revises: b7d2e4f6a8c1
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'c4e6a8b0d2f3'
down_revision: Union[str, None] = 'b7d2e4f6a8c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DIM = 1024  # frozen here on purpose: app/rag/embed.py DIM may change later, this migration may not


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        'documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('practice_id', sa.Integer(), sa.ForeignKey('practices.id'), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('source', sa.String(length=300), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('practice_id', 'source', name='uq_documents_practice_id_source'),
    )
    op.create_index('ix_documents_practice_id', 'documents', ['practice_id'])
    op.create_table(
        'chunks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('token_count', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('embedding', Vector(DIM), nullable=False),
        sa.UniqueConstraint('document_id', 'ordinal', name='uq_chunks_document_id_ordinal'),
    )
    op.create_index('ix_chunks_document_id', 'chunks', ['document_id'])
    op.create_index('ix_chunks_content_hash', 'chunks', ['content_hash'])
    # Keyword side of hybrid search: always in sync with `text`, nobody writes it.
    op.execute("ALTER TABLE chunks ADD COLUMN tsv tsvector "
               "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED")
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    # Cosine distance (<=>), matching how the embeddings are compared.
    op.execute("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    op.drop_table('chunks')
    op.drop_index('ix_documents_practice_id', table_name='documents')
    op.drop_table('documents')
    op.execute("DROP EXTENSION IF EXISTS vector")
