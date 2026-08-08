"""
Support Ticket App — Databricks Lakebase

A support ticket system backed by Lakebase (Databricks-managed Postgres).
Users can view, create, and manage support tickets and messages.
"""

import logging
import os

from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("support-app")

app = Flask(__name__)
_w = WorkspaceClient()


# ──────────────────────────────────────────────
# Database setup
# ──────────────────────────────────────────────

def ensure_tables():
    """Create tables and seed sample data if they don't exist."""
    lakebase.run_write("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    lakebase.run_write("""
        CREATE TABLE IF NOT EXISTS ticket_messages (
            message_id SERIAL PRIMARY KEY,
            ticket_id INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
            message_text TEXT NOT NULL,
            author TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # Seed sample data only if tickets table is empty
    rows = lakebase.run_query("SELECT COUNT(*) AS cnt FROM tickets")
    count = rows[0]["cnt"] if rows else 0
    if count == 0:
        _seed_sample_data()
        logger.info("Seeded sample data into tickets + ticket_messages")


def _seed_sample_data():
    """Insert 3 tickets with 2+ messages each across 3 statuses."""
    # Ticket 1 — open
    lakebase.run_write(
        "INSERT INTO tickets (title, status, created_by) VALUES (%s, %s, %s)",
        ("Cannot access my account after password reset", "open", "alice@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (1, 'I reset my password yesterday but still cannot log in. It keeps saying "invalid credentials".', "alice@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (1, "Hi Alice, can you try clearing your browser cache and attempting again? Also let us know what browser you are using.", "support@example.com"),
    )

    # Ticket 2 — in_progress
    lakebase.run_write(
        "INSERT INTO tickets (title, status, created_by) VALUES (%s, %s, %s)",
        ("API returning 500 errors intermittently", "in_progress", "bob@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (2, "Our production API calls are failing about 20% of the time with HTTP 500. This started around 2pm EST today.", "bob@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (2, "Thanks for reporting. We have identified a degraded backend node and are rerouting traffic. ETA for fix is 30 minutes.", "support@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (2, "Update: the bad node has been removed from the pool. Monitoring for further errors.", "support@example.com"),
    )

    # Ticket 3 — resolved
    lakebase.run_write(
        "INSERT INTO tickets (title, status, created_by) VALUES (%s, %s, %s)",
        ("Feature request: dark mode for dashboard", "resolved", "carol@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (3, "Would love a dark mode option for the analytics dashboard. Late-night reporting is rough on the eyes.", "carol@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (3, "Great suggestion! Dark mode has been shipped in v2.4. You can toggle it in Settings > Appearance.", "support@example.com"),
    )
    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (3, "Amazing, just turned it on. Thank you!", "carol@example.com"),
    )


# ──────────────────────────────────────────────
# Routes — Pages
# ──────────────────────────────────────────────

@app.route("/")
def index():
    """Main support ticket dashboard."""
    return render_template("index.html")


# ──────────────────────────────────────────────
# Routes — API
# ──────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/tickets", methods=["GET"])
def list_tickets():
    """Return all tickets, newest first."""
    rows = lakebase.run_query("""
        SELECT ticket_id, title, status, created_by, created_at
        FROM tickets
        ORDER BY created_at DESC
    """)
    return jsonify(rows)


@app.route("/api/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id):
    """Return a single ticket with all its messages."""
    ticket_rows = lakebase.run_query(
        "SELECT ticket_id, title, status, created_by, created_at FROM tickets WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not ticket_rows:
        return jsonify({"error": "Ticket not found"}), 404

    message_rows = lakebase.run_query(
        "SELECT message_id, ticket_id, message_text, author, created_at FROM ticket_messages WHERE ticket_id = %s ORDER BY created_at ASC",
        (ticket_id,),
    )

    ticket = dict(ticket_rows[0])
    ticket["messages"] = [dict(m) for m in message_rows]
    return jsonify(ticket)


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    """Create a new support ticket."""
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    created_by = (data.get("created_by") or "").strip()

    if not title:
        return jsonify({"error": "Title is required"}), 400
    if not created_by:
        return jsonify({"error": "created_by is required"}), 400

    # Insert the ticket
    lakebase.run_write(
        "INSERT INTO tickets (title, status, created_by) VALUES (%s, %s, %s)",
        (title, "open", created_by),
    )

    # Get the newly created ticket
    rows = lakebase.run_query(
        "SELECT ticket_id, title, status, created_by, created_at FROM tickets ORDER BY ticket_id DESC LIMIT 1"
    )
    return jsonify(dict(rows[0])), 201


@app.route("/api/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_message(ticket_id):
    """Add a message to an existing ticket."""
    data = request.get_json() or {}
    message_text = (data.get("message_text") or "").strip()
    author = (data.get("author") or "").strip()

    if not message_text:
        return jsonify({"error": "message_text is required"}), 400
    if not author:
        return jsonify({"error": "author is required"}), 400

    # Verify ticket exists
    ticket_rows = lakebase.run_query(
        "SELECT ticket_id FROM tickets WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not ticket_rows:
        return jsonify({"error": "Ticket not found"}), 404

    lakebase.run_write(
        "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
        (ticket_id, message_text, author),
    )

    rows = lakebase.run_query(
        "SELECT message_id, ticket_id, message_text, author, created_at FROM ticket_messages WHERE ticket_id = %s ORDER BY message_id DESC LIMIT 1",
        (ticket_id,),
    )
    return jsonify(dict(rows[0])), 201


@app.route("/api/tickets/<int:ticket_id>/status", methods=["PATCH"])
def update_status(ticket_id):
    """Update a ticket's status."""
    data = request.get_json() or {}
    new_status = (data.get("status") or "").strip().lower()

    valid_statuses = {"open", "in_progress", "resolved"}
    if new_status not in valid_statuses:
        return jsonify({"error": f"Status must be one of: {', '.join(sorted(valid_statuses))}"}), 400

    # Verify ticket exists
    ticket_rows = lakebase.run_query(
        "SELECT ticket_id FROM tickets WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not ticket_rows:
        return jsonify({"error": "Ticket not found"}), 404

    lakebase.run_write(
        "UPDATE tickets SET status = %s WHERE ticket_id = %s",
        (new_status, ticket_id),
    )

    rows = lakebase.run_query(
        "SELECT ticket_id, title, status, created_by, created_at FROM tickets WHERE ticket_id = %s",
        (ticket_id,),
    )
    return jsonify(dict(rows[0]))


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
# Main
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# Startup — ensure tables exist and seed data
# ──────────────────────────────────────────────

# Called at import time so tables + sample data are ready before any
# request is served.  This runs once per app startup (not per request).
ensure_tables()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", "8000"))
    app.run(debug=True, host=host, port=port)
