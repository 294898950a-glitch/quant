#!/usr/bin/env python3
"""Measure every registered market fact and write the fact table.

Writes (and is the only writer of):
  data/cb_market_facts/facts.yaml     every fact: statement, status, values, sample, provenance
  data/cb_market_facts/<id>.csv       the breakdown behind each measured fact
  docs/cb-market-structure.txt        the same content as a page, rendered from facts.yaml

A fact with no data is written as not_measured with its reason. A measurement
that raises is written as not_measured with the error; it never disappears.

跑法:
    python scripts/build_cb_market_facts.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cb_market import facts as F  # noqa: E402
from cb_market.events import load_events  # noqa: E402
from cb_market.panel import WAREHOUSE, load_panel  # noqa: E402

OUT_DIR = _REPO_ROOT / "data" / "cb_market_facts"
PAGE = _REPO_ROOT / "docs" / "cb-market-structure.txt"
KIND_TITLES = {
    "contract": "一、合同: 发行时写死的规则",
    "identity": "二、恒等式与边界: 永远成立, 或可以数出违反了多少次",
    "behaviour": "三、行为: 发行人和市场实际怎么做 (会变, 要重测)",
    "environment": "四、环境: 规则、供给、成交",
}


def run() -> dict:
    panel = load_panel()
    inputs = F.Inputs(panel=panel, events=load_events(), terms=pd.read_parquet(WAREHOUSE / "cb_contract_terms.parquet"),
                      holders=pd.read_parquet(WAREHOUSE / "cb_holders_top10.parquet"),
                      balances=pd.read_parquet(WAREHOUSE / "cb_balance_history.parquet"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.csv"):
        old.unlink()
    records = []
    for fact in F.REGISTRY:
        rec = {"id": fact.id, "kind": fact.kind, "statement": fact.statement}
        try:
            result = fact.measure(inputs)
        except Exception as exc:  # a broken measurement is an unanswered fact, not a missing one
            result = F.NotMeasured(f"measurement raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
        if isinstance(result, F.NotMeasured) or result.n <= 0:
            rec.update(status="not_measured", reason=getattr(result, "reason", "empty sample"))
        else:
            rec.update(status="measured", values=F._r(result.values), n=int(result.n),
                       date_range=list(result.date_range), notes=result.notes, detail=None)
            if result.detail is not None:
                name = f"{fact.id}.csv"
                result.detail.round(5).to_csv(OUT_DIR / name, index=False)
                rec["detail"] = name
        records.append(rec)
        print(f"[facts] {fact.id}: {rec['status']}", flush=True)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "cb_market", "scripts"], cwd=_REPO_ROOT,
                                capture_output=True, text=True).stdout.strip())
    doc = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": commit, "code_dirty": dirty,
        "warehouse_end": panel.attrs["end"], "study_start": inputs.study_start,
        "inputs": panel.attrs["inputs"],
        "facts": records,
    }
    (OUT_DIR / "facts.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    return doc


def render(doc: dict) -> str:
    lines = ["转债市场的约束与结构", "====================", "",
             f"由 scripts/build_cb_market_facts.py 生成, 不要手改。数据截止 {doc['warehouse_end']}, "
             f"统计自 {doc['study_start']} 起, 生成于 {doc['generated_at'][:10]}。",
             "每条的数值、样本量和明细在 data/cb_market_facts/ 下。", ""]
    missing = []
    for kind, title in KIND_TITLES.items():
        lines += [title, "-" * 40, ""]
        for rec in (r for r in doc["facts"] if r["kind"] == kind):
            if rec["status"] != "measured":
                missing.append(rec)
                continue
            lines.append(f"[{rec['id']}] {rec['statement']}")
            span = " ~ ".join(d for d in rec["date_range"] if d)
            lines.append(f"    样本 {rec['n']}" + (f", {span}" if span else ""))
            for k, v in rec["values"].items():
                lines.append(f"    {k}: {v}")
            if rec["notes"]:
                lines.append(f"    注: {rec['notes']}")
            lines.append("")
    lines += ["五、没有数据, 不能当事实用", "-" * 40, ""]
    for rec in missing:
        lines += [f"[{rec['id']}] {rec['statement']}", f"    未测量: {rec['reason']}", ""]
    return "\n".join(lines)


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    doc = run()
    PAGE.write_text(render(doc), encoding="utf-8")
    measured = sum(r["status"] == "measured" for r in doc["facts"])
    print(f"[facts] measured {measured} / {len(doc['facts'])}; wrote {OUT_DIR} and {PAGE}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
