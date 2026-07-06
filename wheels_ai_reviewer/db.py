"""
db.py — SQLite persistence for review history.

Backs the dashboard with real data instead of hardcoded placeholder stats.
Every completed review (once it reaches the Final Report step) is saved
here — including the generated DOCX/PDF report bytes — so:
  1. The dashboard can show real totals, file names, scores, and a risk
     distribution, instead of the "128 contracts reviewed" placeholder.
  2. Previously generated reports can be re-downloaded later without
     re-running the review (the whole point of persisting the bytes).

Storage: a single SQLite file alongside app.py. Fine for a pilot/single-app
deployment; swap get_connection()'s path for a shared volume or a real
Postgres/MySQL connection string if this needs to survive container
restarts across multiple app instances.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "review_history.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Idempotent — safe to call on every app startup."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_name  TEXT NOT NULL,
            reviewed_at    TEXT NOT NULL,
            risk_score     INTEGER NOT NULL,
            risk_tier      TEXT NOT NULL,
            fail_count     INTEGER NOT NULL,
            flag_count     INTEGER NOT NULL,
            pass_count     INTEGER NOT NULL,
            docx_report    BLOB,
            pdf_report     BLOB
        )
    """)
    conn.commit()
    conn.close()


def risk_tier(score: int) -> str:
    """Same bucketing used elsewhere in the app — kept here too so the
    dashboard and the report agree on what 'High Risk' means without
    importing app.py (avoids a circular import)."""
    if score == 0:
        return "Low Risk"
    elif score <= 8:
        return "Moderate Risk"
    elif score <= 20:
        return "High Risk"
    return "Critical Risk"


def save_review(
    contract_name: str,
    score: int,
    fail_n: int,
    flag_n: int,
    pass_n: int,
    docx_bytes: bytes,
    pdf_bytes: bytes,
) -> int:
    """Insert one completed review. Returns the new row's id."""
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO reviews
           (contract_name, reviewed_at, risk_score, risk_tier,
            fail_count, flag_count, pass_count, docx_report, pdf_report)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            contract_name,
            datetime.now().isoformat(timespec="seconds"),
            score,
            risk_tier(score),
            fail_n,
            flag_n,
            pass_n,
            docx_bytes,
            pdf_bytes,
        ),
    )
    conn.commit()
    review_id = cur.lastrowid
    conn.close()
    return review_id


def get_review_count() -> int:
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    conn.close()
    return n


def get_average_score() -> float:
    conn = get_connection()
    row = conn.execute("SELECT AVG(risk_score) FROM reviews").fetchone()
    conn.close()
    return round(row[0], 1) if row[0] is not None else 0.0


def get_recent_reviews(limit: int = 10) -> list[dict]:
    """Most recent reviews, WITHOUT the report blobs (keep this cheap to
    call on every dashboard render). Fetch blobs separately via
    get_review_reports() only for rows the user actually wants to download."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, contract_name, reviewed_at, risk_score, risk_tier,
                  fail_count, flag_count, pass_count
           FROM reviews ORDER BY reviewed_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_review_reports(review_id: int) -> dict | None:
    """Fetch the stored DOCX/PDF bytes for one review, for re-download."""
    conn = get_connection()
    row = conn.execute(
        "SELECT contract_name, docx_report, pdf_report FROM reviews WHERE id = ?",
        (review_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_risk_tier_distribution() -> dict[str, int]:
    """Counts per risk tier across ALL reviews — feeds the dashboard pie chart."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT risk_tier, COUNT(*) as n FROM reviews GROUP BY risk_tier"
    ).fetchall()
    conn.close()
    return {r["risk_tier"]: r["n"] for r in rows}