"""
SQLite persistence layer for Contract Compliance Agent.

Tables:
  - runs         : one row per document processed
  - findings     : clause-level findings linked to a run
  - recommendations : per-run recommendation strings
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

DB_PATH = os.getenv("SQLITE_DB_PATH", "database/compliance.db")


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # allow concurrent reads
    conn.execute("PRAGMA busy_timeout=30000") # wait up to 30s on write lock
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ──────────────────────────────────────────────
# Schema Initialisation
# ──────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    file_name           TEXT NOT NULL,
    file_path           TEXT,
    file_type           TEXT,
    file_hash           TEXT,
    file_size           INTEGER,
    document_type       TEXT,
    is_contract         INTEGER,
    confidence          REAL,
    overall_status      TEXT,
    risk_score          INTEGER,
    risk_level          TEXT,
    obligation_count    INTEGER,
    policy_file         TEXT,
    report_path         TEXT,
    json_path           TEXT,
    audit_path          TEXT,
    evidence_json_path  TEXT,
    processing_status   TEXT NOT NULL DEFAULT 'PENDING',
    error_message       TEXT,
    admin_decision      TEXT,
    admin_notes         TEXT,
    admin_decided_at    TEXT,
    admin_reviewer      TEXT,
    created_at          TEXT NOT NULL,
    completed_at        TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    policy_id       TEXT NOT NULL,
    policy_name     TEXT,
    clause_ref      TEXT,
    status          TEXT,
    severity        TEXT,
    finding         TEXT,
    evidence        TEXT,
    image_path      TEXT
);

CREATE TABLE IF NOT EXISTS recommendations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    text    TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create tables if they don't exist and run column migrations."""
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        # Migrate existing runs table if new columns are missing
        existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        for col, ctype in [
            ("admin_decision", "TEXT"),
            ("admin_notes", "TEXT"),
            ("admin_decided_at", "TEXT"),
            ("admin_reviewer", "TEXT"),
        ]:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {ctype}")
        # Clean up any stale processing runs left over from server restarts
        conn.execute("UPDATE runs SET processing_status='FAILED', error_message='Interrupted by server restart' WHERE processing_status='PROCESSING'")


# ──────────────────────────────────────────────
# Runs CRUD
# ──────────────────────────────────────────────

def create_run(run_id: str, file_name: str, file_path: str, policy_file: str) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, file_name, file_path, policy_file, processing_status, created_at)
            VALUES (?, ?, ?, ?, 'PROCESSING', ?)
            """,
            (run_id, file_name, file_path, policy_file, datetime.now(timezone.utc).isoformat()),
        )


def update_run_from_result(run_id: str, result: Any, policy_file: str) -> None:
    """Persist a completed FinalComplianceResult into SQLite."""
    from src.models.schemas import FinalComplianceResult

    r: FinalComplianceResult = result
    now = datetime.now(timezone.utc).isoformat()

    is_contract = r.classification.is_contract
    doc_type = r.classification.document_type.value
    confidence = r.classification.confidence
    overall_status = r.compliance.overall_status if r.compliance else None
    risk_score = r.risk.score if r.risk else None
    risk_level = r.risk.risk_level.value if r.risk else None
    obl_count = len(r.obligations.obligations) if r.obligations else 0
    processing_status = "SKIPPED" if not is_contract else "COMPLETED"

    with get_db() as conn:
        conn.execute(
            """
            UPDATE runs SET
                file_type           = ?,
                file_hash           = ?,
                file_size           = ?,
                document_type       = ?,
                is_contract         = ?,
                confidence          = ?,
                overall_status      = ?,
                risk_score          = ?,
                risk_level          = ?,
                obligation_count    = ?,
                policy_file         = ?,
                report_path         = ?,
                json_path           = ?,
                audit_path          = ?,
                evidence_json_path  = ?,
                processing_status   = ?,
                completed_at        = ?
            WHERE run_id = ?
            """,
            (
                r.document.metadata.file_type.value,
                r.document.metadata.file_hash,
                r.document.metadata.file_size,
                doc_type,
                1 if is_contract else 0,
                confidence,
                overall_status,
                risk_score,
                risk_level,
                obl_count,
                policy_file,
                f"outputs/compliance/{run_id}_report.md",
                f"outputs/compliance/{run_id}_compliance.json",
                f"outputs/audit/{run_id}_audit.json",
                f"outputs/evidence/{run_id}_evidence.json",
                processing_status,
                now,
                run_id,
            ),
        )

        # Save findings
        if r.compliance:
            for f in r.compliance.findings:
                img_path = None
                if r.evidence:
                    for ev in r.evidence.evidence_items:
                        if ev.policy_id == f.policy_id:
                            img_path = ev.image_path
                            break
                conn.execute(
                    """
                    INSERT INTO findings (run_id, policy_id, policy_name, clause_ref, status, severity, finding, evidence, image_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        f.policy_id,
                        f.policy_name,
                        f.clause_reference,
                        f.status.value,
                        f.severity.value,
                        f.finding,
                        f.evidence,
                        img_path,
                    ),
                )

        # Save recommendations
        for rec in r.recommendations:
            conn.execute(
                "INSERT INTO recommendations (run_id, text) VALUES (?, ?)",
                (run_id, rec),
            )


def mark_run_failed(run_id: str, error: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE runs SET processing_status='FAILED', error_message=?, completed_at=? WHERE run_id=?",
            (error, datetime.now(timezone.utc).isoformat(), run_id),
        )


def get_all_runs(limit: int = 200, offset: int = 0) -> List[Dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_run(run_id: str) -> Optional[Dict]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["findings"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM findings WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        ]
        run["recommendations"] = [
            r["text"] for r in conn.execute(
                "SELECT text FROM recommendations WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        ]
        return run


def delete_run(run_id: str) -> bool:
    with get_db() as conn:
        result = conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        return result.rowcount > 0


def get_stats() -> Dict:
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        contracts = conn.execute("SELECT COUNT(*) FROM runs WHERE is_contract=1").fetchone()[0]
        passed = conn.execute("SELECT COUNT(*) FROM runs WHERE overall_status='PASS'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM runs WHERE overall_status='FAIL'").fetchone()[0]
        risk = conn.execute("SELECT COUNT(*) FROM runs WHERE overall_status='RISK'").fetchone()[0]
        avg_score = conn.execute("SELECT AVG(risk_score) FROM runs WHERE risk_score IS NOT NULL").fetchone()[0]
        return {
            "total_runs": total,
            "contracts_processed": contracts,
            "passed": passed,
            "failed": failed,
            "at_risk": risk,
            "avg_risk_score": round(avg_score or 0, 1),
        }


def set_admin_decision(run_id: str, decision: str, notes: str = "", reviewer: str = "admin@compliance.ai") -> bool:
    """Set or update administrative decision on a flagged contract run."""
    with get_db() as conn:
        now = datetime.now(timezone.utc).isoformat()
        new_status = "APPROVED" if decision == "APPROVED" else ("REJECTED" if decision == "REJECTED" else None)
        if new_status:
            res = conn.execute(
                """
                UPDATE runs SET admin_decision=?, admin_notes=?, admin_decided_at=?, admin_reviewer=?, overall_status=?
                WHERE run_id=?
                """,
                (decision, notes, now, reviewer, new_status, run_id),
            )
        else:
            res = conn.execute(
                """
                UPDATE runs SET admin_decision=?, admin_notes=?, admin_decided_at=?, admin_reviewer=?
                WHERE run_id=?
                """,
                (decision, notes, now, reviewer, run_id),
            )
        return res.rowcount > 0


def get_flagged_runs() -> List[Dict]:
    """Return all flagged runs (RISK, FAIL, APPROVED, or with admin_decision) for real-time admin review desk."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE (overall_status IN ('RISK', 'FAIL', 'APPROVED') OR processing_status = 'FAILED' OR admin_decision IS NOT NULL)
            ORDER BY created_at DESC
            """
        ).fetchall()
        flagged = []
        for r in rows:
            d = dict(r)
            d["findings_count"] = conn.execute(
                "SELECT COUNT(*) FROM findings WHERE run_id=?", (d["run_id"],)
            ).fetchone()[0]
            flagged.append(d)
        return flagged


def get_admin_stats() -> Dict:
    """Return key metrics specifically for the admin flagged decisions dashboard."""
    with get_db() as conn:
        flagged_count = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE overall_status IN ('RISK', 'FAIL') OR processing_status='FAILED'"
        ).fetchone()[0]
        pending_review = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE (overall_status IN ('RISK', 'FAIL') OR processing_status='FAILED') AND (admin_decision IS NULL OR admin_decision = '' OR admin_decision = 'PENDING')"
        ).fetchone()[0]
        approved = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE admin_decision = 'APPROVED'"
        ).fetchone()[0]
        rejected = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE admin_decision = 'REJECTED'"
        ).fetchone()[0]
        amendment = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE admin_decision = 'AMENDMENT_REQUESTED'"
        ).fetchone()[0]
        return {
            "flagged_total": flagged_count,
            "pending_review": pending_review,
            "approved": approved,
            "rejected": rejected,
            "amendment_requested": amendment,
            "decided_total": approved + rejected + amendment,
        }

