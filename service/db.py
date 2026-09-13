"""SQLite state store for the cloud-agents dispatcher."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id              TEXT PRIMARY KEY,
  type            TEXT NOT NULL DEFAULT 'coding',
  repo            TEXT NOT NULL DEFAULT 'scratch',
  base            TEXT NOT NULL DEFAULT 'main',
  task            TEXT NOT NULL,
  engine_requested TEXT NOT NULL DEFAULT 'auto',
  engine_selected TEXT,
  engine_reason   TEXT,
  status          TEXT NOT NULL DEFAULT 'queued',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  budget_usd      REAL,
  max_minutes     INTEGER,
  idempotency_key TEXT UNIQUE,
  created_at      REAL,
  updated_at      REAL,
  started_at      REAL,
  finished_at     REAL,
  result_json     TEXT,
  error           TEXT
);
CREATE TABLE IF NOT EXISTS events (
  seq    INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  ts     REAL,
  kind   TEXT,
  msg    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

ACTIVE = ("queued", "preparing", "running", "validating", "needs_attention")
TERMINAL = ("completed", "failed", "cancelled")

# Columns added after the initial schema. Applied idempotently on connect.
_MIGRATIONS = [
    ("jobs", "provider", "TEXT NOT NULL DEFAULT 'supergrok'"),
    ("jobs", "attention_flagged_at", "REAL"),
    ("jobs", "attention_acked_at", "REAL"),
    ("jobs", "spend_usd", "REAL"),
    ("jobs", "kill_reason", "TEXT"),
    ("jobs", "or_baseline_usd", "REAL"),
    # Mid-flight SuperGrok -> OpenRouter failover record.
    ("jobs", "failover_at", "REAL"),
    ("jobs", "failover_from", "TEXT"),
    ("jobs", "failover_to", "TEXT"),
    ("jobs", "failover_reason", "TEXT"),
    # Git ref requested at submit (branch/tag/pull/N/head) for host-seeded repos.
    ("jobs", "ref", "TEXT"),
]

_SPEND_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_ledger (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  job_id  TEXT,
  usd     REAL NOT NULL,
  source  TEXT NOT NULL,   -- 'or_key_delta' | 'token_estimate' | 'manual'
  note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_spend_ledger_ts ON spend_ledger(ts);
"""

_TOKEN_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                REAL NOT NULL,
  job_id            TEXT,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  provider          TEXT,   -- 'supergrok' | 'openrouter'
  model             TEXT
);
CREATE INDEX IF NOT EXISTS idx_token_usage_job ON token_usage(job_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for table, col, ddl in _MIGRATIONS:
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(_SPEND_LEDGER_SCHEMA)
    conn.executescript(_TOKEN_USAGE_SCHEMA)
    _migrate(conn)
    return conn


def new_job_id() -> str:
    return "job_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def insert_job(conn, *, job_id, type, repo, base, task, engine_requested,
               budget_usd, max_minutes, idempotency_key=None, provider="supergrok",
               ref=None) -> dict:
    now = time.time()
    conn.execute(
        """INSERT INTO jobs
           (id,type,repo,base,task,engine_requested,status,budget_usd,max_minutes,
            idempotency_key,created_at,updated_at,provider,ref)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, type, repo, base, task, engine_requested, "queued",
         budget_usd, max_minutes, idempotency_key, now, now, provider, ref),
    )
    conn.commit()
    return get_job(conn, job_id)


def get_job(conn, job_id) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def find_by_idempotency(conn, key) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
    return dict(row) if row else None


def update_job(conn, job_id, **fields) -> None:
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def log_event(conn, job_id, kind, msg) -> None:
    conn.execute(
        "INSERT INTO events (job_id,ts,kind,msg) VALUES (?,?,?,?)",
        (job_id, time.time(), kind, msg),
    )
    conn.commit()


def list_jobs(conn, status=None, limit=50) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_active(conn) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) c FROM jobs WHERE status IN ({','.join('?'*len(ACTIVE))})",
        ACTIVE,
    ).fetchone()
    return row["c"]


def claim_next(conn, job_id: str) -> bool:
    """Atomically move a queued job to preparing. Returns True if we won."""
    cur = conn.execute(
        "UPDATE jobs SET status='preparing', started_at=?, updated_at=? "
        "WHERE id=? AND status='queued'",
        (time.time(), time.time(), job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def next_queued(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Attention queue (hang detection) and spend ledger.
# ---------------------------------------------------------------------------

def flag_attention(conn, job_id: str, reason: str) -> bool:
    """Move a running job to needs_attention. Returns True if it transitioned."""
    now = time.time()
    cur = conn.execute(
        "UPDATE jobs SET status='needs_attention', attention_flagged_at=?, "
        "attention_acked_at=NULL, updated_at=? "
        "WHERE id=? AND status IN ('running','validating')",
        (now, now, job_id),
    )
    conn.commit()
    if cur.rowcount:
        log_event(conn, job_id, "attention", f"flagged: {reason}")
    return cur.rowcount == 1


def ack_attention(conn, job_id: str) -> bool:
    """Acknowledge a needs_attention job; it may run to its hard ceiling."""
    job = get_job(conn, job_id)
    if not job or job["status"] != "needs_attention":
        return False
    update_job(conn, job_id, attention_acked_at=time.time())
    log_event(conn, job_id, "attention", "acknowledged by operator; runs to ceiling")
    return True


def attention_queue(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='needs_attention' "
        "ORDER BY attention_flagged_at"
    ).fetchall()
    return [dict(r) for r in rows]


def record_spend(conn, job_id: str | None, usd: float, source: str,
                 note: str = "") -> None:
    conn.execute(
        "INSERT INTO spend_ledger (ts,job_id,usd,source,note) VALUES (?,?,?,?,?)",
        (time.time(), job_id, usd, source, note),
    )
    conn.commit()


def rolling_spend_usd(conn, days: float = 7.0) -> float:
    """Rolling cash-truth spend: only 'or_key_delta' rows count toward the cap.

    'token_estimate' rows are per-job attribution recorded for reconciliation;
    they must NOT inflate the cap or spend would be double-counted.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(usd),0) s FROM spend_ledger "
        "WHERE ts > ? AND source='or_key_delta'",
        (time.time() - days * 86400,),
    ).fetchone()
    return float(row["s"])


def running_jobs(conn) -> list[dict]:
    """Jobs with a live worker: running, validating, or needs_attention."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status IN ('running','validating','needs_attention')"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Token telemetry (per-request usage captured by the broker).
# ---------------------------------------------------------------------------

def record_tokens(conn, job_id: str, prompt_tokens: int,
                  completion_tokens: int, provider: str, model: str) -> None:
    conn.execute(
        "INSERT INTO token_usage "
        "(ts,job_id,prompt_tokens,completion_tokens,provider,model) "
        "VALUES (?,?,?,?,?,?)",
        (time.time(), job_id, int(prompt_tokens), int(completion_tokens),
         provider, model),
    )
    conn.commit()


def token_totals(conn, job_id: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens),0) p, "
        "COALESCE(SUM(completion_tokens),0) c, COUNT(*) n "
        "FROM token_usage WHERE job_id=?",
        (job_id,),
    ).fetchone()
    return {"prompt_tokens": int(row["p"]),
            "completion_tokens": int(row["c"]),
            "requests": int(row["n"])}


def token_breakdown(conn, job_id: str) -> list[dict]:
    """Per (provider, model) token totals for one job — for pricing."""
    rows = conn.execute(
        "SELECT provider, model, COALESCE(SUM(prompt_tokens),0) p, "
        "COALESCE(SUM(completion_tokens),0) c, COUNT(*) n "
        "FROM token_usage WHERE job_id=? GROUP BY provider, model",
        (job_id,),
    ).fetchall()
    return [{"provider": r["provider"], "model": r["model"],
             "prompt_tokens": int(r["p"]), "completion_tokens": int(r["c"]),
             "requests": int(r["n"])} for r in rows]
