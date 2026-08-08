"""Embed weather documents that do not yet have vector chunks."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from psycopg2.extras import execute_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lakebase

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
MODEL_NAME = "all-MiniLM-L6-v2"
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"

logger = logging.getLogger(__name__)


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping character windows."""
    if not text:
        return []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks = []
    for start in range(0, len(text), step):
        chunk = text[start:start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        if start + CHUNK_SIZE >= len(text):
            break
    return chunks


def _vector_literal(values) -> str:
    return "[" + ",".join(f"{float(value):.12f}" for value in values) + "]"


def ingest_weather_embeddings() -> dict[str, int]:
    """Embed unembedded weather documents and persist chunk vectors."""
    from sentence_transformers import SentenceTransformer

    lakebase.ensure_weather_tables()
    documents = lakebase.run_query(
        """
        SELECT d.id, d.narrative_text
        FROM weather_documents d
        LEFT JOIN weather_embeddings e ON e.document_id = d.id
        WHERE e.document_id IS NULL
        ORDER BY d.id
        """
    )

    logger.info("Found %d weather documents without embeddings", len(documents))
    if not documents:
        return {"embedded": 0, "chunks": 0}

    model = SentenceTransformer(MODEL_ID)
    insert_rows: list[tuple[str, int, str, str, str]] = []
    embedded_docs = 0

    for document in documents:
        chunks = chunk_text(document["narrative_text"])
        if not chunks:
            continue
        vectors = model.encode(chunks, normalize_embeddings=True)
        embedded_docs += 1
        for chunk_index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            insert_rows.append(
                (
                    document["id"],
                    chunk_index,
                    chunk,
                    _vector_literal(vector.tolist()),
                    MODEL_NAME,
                )
            )

    if insert_rows:
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO weather_embeddings (
                        document_id, chunk_index, chunk_text, embedding, model_name
                    ) VALUES %s
                    ON CONFLICT (document_id, chunk_index) DO NOTHING
                    """,
                    insert_rows,
                    template="(%s, %s, %s, %s::vector, %s)",
                    page_size=250,
                )
                conn.commit()

    logger.info("Embedded %d weather documents into %d chunks", embedded_docs, len(insert_rows))
    return {"embedded": embedded_docs, "chunks": len(insert_rows)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Embedding weather documents with chunk size=%d overlap=%d", CHUNK_SIZE, CHUNK_OVERLAP)
    result = ingest_weather_embeddings()
    logger.info("Completed weather embedding ingestion: %s", result)
