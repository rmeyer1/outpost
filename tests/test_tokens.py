#!/usr/bin/env python3
"""Tests for token-level per-job spend estimates (Tier 1 policy).

Covers: pricing lookup math, record/token_totals/token_breakdown, the
estimate_job_usd attribution, and the guarantee that token estimates do
NOT count toward the $25 rolling cap (only or_key_delta does).

Run: python3 tests/test_tokens.py
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

SCRATCH = Path(tempfile.mkdtemp(prefix="ca-tokens-"))
shutil.copytree(ROOT / "service", SCRATCH / "service")
shutil.copytree(ROOT / "config", SCRATCH / "config")
sys.path.insert(0, str(SCRATCH / "service"))
for m in [m for m in list(sys.modules) if m in
          ("db", "pricing", "redact", "openrouter", "proxy_broker")]:
    del sys.modules[m]

import yaml  # noqa: E402
import db  # noqa: E402
import pricing  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{detail}]" if detail and not cond else ""))


with open(SCRATCH / "config" / "spend.yaml") as f:
    spend_cfg = yaml.safe_load(f)

# ------------------------------------------------------------------ pricing

pi, po, src = pricing.lookup_price(
    spend_cfg, "openrouter", "deepseek/deepseek-v4-flash-0731")
check("deepseek input rate", pi == 0.03, pi)
check("deepseek output rate", po == 0.07, po)
check("deepseek source tag", src == "token_estimate", src)

pi, po, src = pricing.lookup_price(spend_cfg, "supergrok", "grok-4.6")
check("supergrok prices at $0", pi == 0.0 and po == 0.0, (pi, po))
check("supergrok source tag", src == "subscription", src)

# unknown provider/model -> unpriced
pi, po, src = pricing.lookup_price(spend_cfg, "nope", "nope")
check("unknown pricing returns zeros",
      pi == 0.0 and po == 0.0 and src == "unpriced", (pi, po, src))

# real math check with the deepseek rates:
usd, src = pricing.estimate_usd(
    spend_cfg, "openrouter", "deepseek/deepseek-v4-flash-0731",
    1_000_000, 1_000_000)
check("1M/1M deepseek tokens = $0.10", abs(usd - 0.10) < 1e-12
      and src == "token_estimate", (usd, src))

# ------------------------------------------------------------------ ledger

db_path = str(SCRATCH / "state.sqlite")
conn = db.connect(db_path)
db.insert_job(conn, job_id="job-t1", type="code", repo="https://x/y",
              base="main", task="t", engine_requested="hermes",
              budget_usd=0, max_minutes=60, provider="openrouter")

db.record_tokens(conn, "job-t1", 1000, 200, provider="openrouter",
                 model="deepseek/deepseek-v4-flash-0731")
db.record_tokens(conn, "job-t1", 500, 100, provider="openrouter",
                 model="deepseek/deepseek-v4-flash-0731")
db.record_tokens(conn, "job-t1", 2000, 0, provider="supergrok",
                 model="grok-4.6")

t = db.token_totals(conn, "job-t1")
check("token_totals sums", t["prompt_tokens"] == 3500
      and t["completion_tokens"] == 300 and t["requests"] == 3, str(t))

b = db.token_breakdown(conn, "job-t1")
check("token_breakdown groups by provider/model",
      len(b) == 2, str(b))
by_pm = {(r["provider"], r["model"]): r for r in b}
check("breakdown openrouter row",
      by_pm[("openrouter", "deepseek/deepseek-v4-flash-0731")][
          "prompt_tokens"] == 1500)
check("breakdown supergrok row",
      by_pm[("supergrok", "grok-4.6")]["prompt_tokens"] == 2000)

est_usd, detail = pricing.estimate_job_usd(spend_cfg, b)
expected = (1500 / 1e6 * 0.03 + 300 / 1e6 * 0.07) + 0.0
check("estimate_job_usd sums priced rows, supergrok free",
      abs(est_usd - expected) < 1e-12, (est_usd, expected))
check("estimate detail carries source tags",
      all(d["source"] in ("token_estimate", "subscription") for d in detail)
      and any(d["source"] == "subscription" for d in detail))

# ------------------------------------------------- cap counts cash truth only

db.record_spend(conn, "job-t1", 5.0, "or_key_delta", "measured delta")
db.record_spend(conn, "job-t1", 100.0, "token_estimate", "attribution only")
db.record_spend(conn, "job-t1", 7.0, "or_key_delta", "another delta")
spend = db.rolling_spend_usd(conn, 7.0)
check("rolling cap ignores token_estimate rows", abs(spend - 12.0) < 1e-9,
      spend)
conn.close()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if FAIL else 0)
