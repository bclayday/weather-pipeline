"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL (a standard Postgres connection URL,
e.g. postgresql://role:password@host:5432/databricks_postgres?sslmode=require)
pointing at a native Postgres role with a static, non-expiring password.
This keeps setup to a single secret instead of five separate env vars.
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import Json, RealDictCursor
from sqlalchemy import create_engine

_w = WorkspaceClient()

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _lakebase_url() -> str:
    """Fetch and decode the Lakebase connection URL from the Databricks secret scope."""
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase."""
    return create_engine(_lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def ensure_weather_tables():
    """Create weather document and embedding tables plus pgvector index."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_documents (
                    id TEXT PRIMARY KEY,
                    location TEXT,
                    source_type TEXT,
                    headline TEXT,
                    narrative_text TEXT NOT NULL,
                    severity TEXT,
                    event_type TEXT,
                    issued_at TIMESTAMPTZ,
                    effective_at TIMESTAMPTZ,
                    expires TIMESTAMPTZ,
                    payload JSONB,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_embeddings (
                    id SERIAL PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    chunk_text TEXT NOT NULL,
                    embedding vector(384) NOT NULL,
                    model_name TEXT NOT NULL DEFAULT 'all-MiniLM-L6-v2',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (document_id, chunk_index)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_weather_embeddings_hnsw
                ON weather_embeddings USING hnsw (embedding vector_cosine_ops)
                """
            )
            conn.commit()


def upsert_weather_document(doc: dict) -> int:
    """Insert or update a weather document and return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO weather_documents (
                    id, location, source_type, headline, narrative_text,
                    severity, event_type, issued_at, effective_at, expires,
                    payload, synced_at
                ) VALUES (
                    %(id)s, %(location)s, %(source_type)s, %(headline)s, %(narrative_text)s,
                    %(severity)s, %(event_type)s, %(issued_at)s, %(effective_at)s, %(expires)s,
                    %(payload)s, now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    location = EXCLUDED.location,
                    source_type = EXCLUDED.source_type,
                    headline = EXCLUDED.headline,
                    narrative_text = EXCLUDED.narrative_text,
                    severity = EXCLUDED.severity,
                    event_type = EXCLUDED.event_type,
                    issued_at = EXCLUDED.issued_at,
                    effective_at = EXCLUDED.effective_at,
                    expires = EXCLUDED.expires,
                    payload = EXCLUDED.payload,
                    synced_at = now()
                """,
                {
                    **doc,
                    "payload": Json(doc.get("payload")),
                },
            )
            conn.commit()
            return cur.rowcount
