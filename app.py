"""
Weather Intelligence App — Databricks Lakebase

Harvests unstructured weather alerts from the NWS API, chunks and embeds
the narrative text using sentence-transformers (all-MiniLM-L6-v2, 384-dim),
and serves semantic vector search via pgvector on Lakebase.
"""

import logging
import os
import threading

from flask import Flask, jsonify, render_template, request

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-app")

app = Flask(__name__)
_weather_model = None
_weather_model_lock = threading.Lock()


def _get_weather_model():
    """Load the embedding model lazily on first weather-search request."""
    global _weather_model
    if _weather_model is None:
        with _weather_model_lock:
            if _weather_model is None:
                from sentence_transformers import SentenceTransformer
                _weather_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _weather_model


def _vector_literal(values) -> str:
    return "[" + ",".join(f"{float(v):.12f}" for v in values) + "]"


# ──────────────────────────────────────────────
# Routes — Pages
# ──────────────────────────────────────────────

@app.route("/")
def index():
    """Weather intelligence dashboard."""
    return render_template("weather.html")


@app.route("/weather")
def weather_page():
    """Weather intelligence dashboard (alias)."""
    return render_template("weather.html")


# ──────────────────────────────────────────────
# Routes — API
# ──────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ──────────────────────────────────────────────
# Routes — Weather API
# ──────────────────────────────────────────────

@app.route("/weather/sync", methods=["POST"])
def sync_weather():
    """Fetch alerts and forecasts for requested locations and upsert them."""
    from weather_client import DEFAULT_LOCATIONS, sync_locations

    lakebase.ensure_weather_tables()
    data = request.get_json(silent=True) or {}
    locations = data.get("locations") or DEFAULT_LOCATIONS
    limit = data.get("limit", 50)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    result = sync_locations(locations=locations, limit=limit)
    synced = 0
    for document in result["documents"]:
        synced += lakebase.upsert_weather_document(document)

    return jsonify({"synced": synced, "errors": result["errors"], "locations": result["requested_locations"]})


@app.route("/weather/embed", methods=["POST"])
def embed_weather():
    """Trigger embedding generation for unembedded weather documents."""
    from scripts.ingest_weather_embeddings import ingest_weather_embeddings

    result = ingest_weather_embeddings()
    return jsonify({"embedded": result["embedded"], "chunks": result["chunks"]})


@app.route("/weather/search", methods=["POST"])
def search_weather():
    """Semantically search embedded weather chunks."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    try:
        top_k = int(data.get("top_k", 5))
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(top_k, 20))

    lakebase.ensure_weather_tables()
    counts = lakebase.run_query("SELECT COUNT(*) AS cnt FROM weather_embeddings")
    if not counts or counts[0]["cnt"] == 0:
        return jsonify({"results": [], "message": "No weather embeddings found yet. Run /weather/sync and /weather/embed first."})

    query_vector = _vector_literal(_get_weather_model().encode(query, normalize_embeddings=True).tolist())
    rows = lakebase.run_query(
        """
        SELECT d.id, d.location, d.headline, d.narrative_text, e.chunk_text,
               1 - (e.embedding <=> %s::vector) AS similarity
        FROM weather_embeddings e
        JOIN weather_documents d ON d.id = e.document_id
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s;
        """,
        (query_vector, query_vector, top_k),
    )
    return jsonify({"results": [dict(row) for row in rows]})


# ──────────────────────────────────────────────
# Error handler
# ──────────────────────────────────────────────

@app.errorhandler(Exception)
def handle_exception(err):
    logger.exception("Unhandled exception")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


# ──────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────

# Ensure weather tables exist on startup
lakebase.ensure_weather_tables()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", "8000"))
    app.run(debug=True, host=host, port=port)
