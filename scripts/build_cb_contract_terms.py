#!/usr/bin/env python3
"""Build cb_contract_terms.parquet: each bond's contract terms, with the clause
text they were read from.

One row per bond: conditional call (window / required days / trigger),
conditional put, redemption price at maturity, coupon schedule, conversion
period. A term that cannot be read is null and its *_status column says
parsed / no_clause / unparsed / no_source. The clause text is stored next to
the parsed values so any row can be checked by eye.

跑法:
    python scripts/build_cb_contract_terms.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cb_market.contracts import parse_down_revision_clause, parse_terms  # noqa: E402
from scripts.build_cb_warehouse import WAREHOUSE_DIR, code_to_ts_code, fetch_cb_universe_em, to_ymd  # noqa: E402

# written by scripts/fetch_cb_revision_clause_text.py: per bond, the clause sentence from an issuer notice
REVISION_CLAUSE_TEXT = WAREHOUSE_DIR / "cb_revision_clause_text.jsonl"

STATUS_COLUMNS = ["call_status", "maturity_redemption_status", "put_status", "coupon_status", "down_revision_status"]


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    raw = fetch_cb_universe_em()
    rows = []
    for r in raw:
        code = str(r.get("SECURITY_CODE") or "").strip()
        if not (code.isdigit() and len(code) == 6):
            continue
        rows.append({
            "ts_code": code_to_ts_code(code), "code": code, "name": r.get("SECURITY_NAME_ABBR"),
            "value_date": to_ymd(r.get("VALUE_DATE")),
            "conversion_start_date": to_ymd(r.get("TRANSFER_START_DATE")),
            "initial_conv_price": pd.to_numeric(r.get("INITIAL_TRANSFER_PRICE"), errors="coerce"),
            "issue_size_yi": pd.to_numeric(r.get("ACTUAL_ISSUE_SCALE"), errors="coerce"),
            **parse_terms(r.get("REDEEM_CLAUSE"), r.get("RESALE_CLAUSE"), r.get("INTEREST_RATE_EXPLAIN")),
            "redeem_clause_text": r.get("REDEEM_CLAUSE"),
            "resale_clause_text": r.get("RESALE_CLAUSE"),
            "coupon_text": r.get("INTEREST_RATE_EXPLAIN"),
        })
    out = pd.DataFrame(rows).drop_duplicates("ts_code").sort_values("ts_code").reset_index(drop=True)

    # down-revision clause: a bond with no notice restating it is no_source, never a market default
    clause = {}
    with REVISION_CLAUSE_TEXT.open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("sentence") or rec["ts_code"] not in clause:  # append-only file: a found sentence wins
                clause[rec["ts_code"]] = rec
    revision = pd.DataFrame([
        {**parse_down_revision_clause((clause.get(ts) or {}).get("sentence")),
         "down_revision_clause_text": (clause.get(ts) or {}).get("sentence"),
         "down_revision_clause_source": (clause.get(ts) or {}).get("pdf_url")}
        for ts in out["ts_code"]
    ])
    out = pd.concat([out, revision], axis=1)
    out.to_parquet(WAREHOUSE_DIR / "cb_contract_terms.parquet", index=False)

    call = out[out["call_status"] == "parsed"]
    put = out[out["put_status"] == "parsed"]
    combos = call.groupby(["call_window", "call_required_days", "call_trigger_pct"]).size().sort_values(ascending=False)
    meta = {
        "name": "cb_contract_terms",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": "eastmoney RPT_BOND_CB_LIST clause text (REDEEM_CLAUSE, RESALE_CLAUSE, INTEREST_RATE_EXPLAIN); "
                  "down-revision clause from issuer notices on cninfo (cb_revision_clause_text.jsonl)",
        "bonds": int(len(out)),
        "status_counts": {c: {k: int(v) for k, v in out[c].value_counts().items()} for c in STATUS_COLUMNS},
        "call_terms": [
            {"window": int(w), "required_days": int(n), "trigger_pct": float(t), "bonds": int(v)}
            for (w, n, t), v in combos.items()
        ],
        "put_trigger_pct": {str(k): int(v) for k, v in put["put_trigger_pct"].value_counts().items()},
        "put_period": {str(k): int(v) for k, v in put["put_period"].value_counts().items()},
        "maturity_redemption_price_quantiles": {
            str(q): float(v) for q, v in out["maturity_redemption_price"].quantile([0.05, 0.25, 0.5, 0.75, 0.95]).items()
        },
        "down_revision_terms": [
            {"window": int(w), "required_days": int(n), "trigger_pct": float(t), "bonds": int(v)}
            for (w, n, t), v in out[out["down_revision_status"] == "parsed"]
            .groupby(["revision_window", "revision_required_days", "revision_trigger_pct"]).size()
            .sort_values(ascending=False).items()
        ],
        "not_available": {
            "down_revision_clause": "read only for bonds whose issuer published a notice restating it (possible or "
                                    "declined revision); the rest are no_source. The floor on the new price and the "
                                    "shareholder-vote rule are not parsed.",
            "call_redemption_price": "not parsed; most bonds pay par plus accrued interest, some older ones a fixed 103-105",
        },
    }
    (WAREHOUSE_DIR / "cb_contract_terms.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: meta[k] for k in ("bonds", "status_counts", "call_terms", "down_revision_terms")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
