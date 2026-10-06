"""Dated issuer events: down-revision and call decisions.

One row per event. The file is built by scripts/build_cb_events.py from three
sources and is the only place that says "on this date the issuer did X".
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

EVENTS_PARQUET = Path(__file__).resolve().parent.parent / "data" / "cb_warehouse" / "cb_events.parquet"

REVISION_PROPOSAL = "revision_proposal"      # board proposes a down-revision (cninfo)
REVISION_DECIDED = "revision_decided"        # final notice of the new conversion price (cninfo)
NO_REVISION = "no_revision"                  # board decides not to revise (cninfo)
REVISION_MAY_TRIGGER = "revision_may_trigger"  # notice that the trigger is about to be met (cninfo)
REVISION_MEETING = "revision_meeting"        # shareholder meeting; detail = approved/rejected/cancelled (jisilu)
REVISION_EFFECTIVE = "revision_effective"    # new conversion price takes effect (jisilu)
CALL_ANNOUNCED = "call_announced"            # forced call (cb_call.parquet)
NO_CALL = "no_call"                          # issuer announces it will not call (cninfo)
CALL_MAY_TRIGGER = "call_may_trigger"        # notice that the call trigger is about to be met (cninfo)
UNCLASSIFIED = "unclassified"


def classify_revision_title(title: str) -> str:
    """Type of a cninfo announcement found under the keyword 下修."""
    if re.search("不向下修正|不下修|不修正|暂不|不行使", title):
        return NO_REVISION
    if re.search("预计|可能|即将", title) and re.search("触发|满足", title):
        return REVISION_MAY_TRIGGER
    if re.search("提议|建议", title):
        return REVISION_PROPOSAL
    if re.search("向下修正.{0,20}转股价格|修正.{0,12}转股价格|下修", title) and not re.search("条件|条款|提示", title):
        return REVISION_DECIDED
    return UNCLASSIFIED


def load_events() -> pd.DataFrame:
    """ts_code, event_date (YYYYMMDD), event_type, source, detail."""
    return pd.read_parquet(EVENTS_PARQUET)
