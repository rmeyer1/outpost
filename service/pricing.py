"""Token-based spend estimation.

Pricing lives in config/spend.yaml under `pricing:`. The estimates are the
per-job ATTRIBUTION layer: the $25 OpenRouter cap is always enforced from
key-usage deltas (cash truth); the estimate is reconciled against the delta
at finalize time and any drift is noted in the ledger.
"""
from __future__ import annotations


def lookup_price(spend_cfg: dict, provider: str,
                 model: str | None) -> tuple[float, float, str]:
    """Return (input_per_m_usd, output_per_m_usd, source).

    Unknown provider/model -> (0.0, 0.0, 'unpriced'). SuperGrok is the
    subscription tier -> (0.0, 0.0, 'subscription').
    """
    pricing = (spend_cfg.get("pricing") or {})
    entry = (pricing.get(provider) or {})
    if provider == "supergrok":
        return (float(entry.get("input_per_m_usd", 0.0)),
                float(entry.get("output_per_m_usd", 0.0)),
                str(entry.get("source", "subscription")))
    models = entry.get("models") or {}
    m = models.get(model or "")
    if not m:
        # Single-model tier: fall back to the only priced model so a renamed
        # model string doesn't silently zero the estimate.
        if len(models) == 1:
            m = next(iter(models.values()))
        else:
            return 0.0, 0.0, "unpriced"
    return (float(m.get("input_per_m_usd", 0.0)),
            float(m.get("output_per_m_usd", 0.0)),
            str(entry.get("source", "token_estimate")))


def estimate_usd(spend_cfg: dict, provider: str, model: str | None,
                 prompt_tokens: int, completion_tokens: int
                 ) -> tuple[float, str]:
    """Estimate USD for a token count. Returns (usd, source)."""
    inp, outp, source = lookup_price(spend_cfg, provider, model)
    usd = (prompt_tokens / 1_000_000) * inp + \
          (completion_tokens / 1_000_000) * outp
    return round(usd, 6), source


def estimate_job_usd(spend_cfg: dict, breakdown: list[dict]) -> tuple[float, dict]:
    """Sum estimates over token_breakdown() rows.

    Returns (total_usd, per_row_details).
    """
    total = 0.0
    details = []
    for row in breakdown:
        usd, source = estimate_usd(
            spend_cfg, row.get("provider") or "", row.get("model"),
            row.get("prompt_tokens", 0), row.get("completion_tokens", 0))
        total += usd
        details.append({**row, "estimated_usd": usd, "source": source})
    return round(total, 6), details
