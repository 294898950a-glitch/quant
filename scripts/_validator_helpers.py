"""共享 validator helper: fail 时给出可操作提示, 让 AI 自己找路.

用法:
    from scripts._validator_helpers import FailReport

    if not baseline_rows:
        fr = FailReport(
            what="Q5 baseline trade rows missing",
            looked_for="candidate == 'medium_baseline'",
        )
        fr.with_columns(trades_all)
        fr.with_sample(trades_all[:2])
        fr.fuzzy_match_keys(["candidate", "type"], trades_all)
        fr.add_hint("如果 column 实际叫 X 不叫 Y, 改代码 r.get('X')")
        return fr.format()

输出:
    ERROR Q5 baseline trade rows missing
      Looked for: candidate == 'medium_baseline'
      Actual columns: ['name', 'kind', 'cb_code', ...]
      Sample first 2 rows:
        {name: medium_baseline, kind: baseline, ...}
        {name: breadth_xxx, kind: selected, ...}
      Fuzzy match:
        'candidate' → maybe 'name' (similarity 0.40)
        'type' → maybe 'kind' (similarity 0.50)
      Hint: 如果 column 实际叫 X 不叫 Y, 改代码 r.get('X')

哲学: 不给"逃生口", 给"看清事实 + 自己找路". AI 反复 fail 时看到实际数据
就能定位, 不需要绕过 validator.
"""

from __future__ import annotations

import difflib
import json
from typing import Any


class FailReport:
    """收集 fail 时的可操作信息, 格式化输出给 AI 看."""

    def __init__(self, what: str, looked_for: str):
        self.what = what
        self.looked_for = looked_for
        self._actual_schema: list[str] | None = None
        self._sample_rows: list[Any] | None = None
        self._fuzzy_hints: list[str] = []
        self._extra_hints: list[str] = []

    def with_columns(self, rows: list[dict] | None) -> "FailReport":
        """从 dict list 取 column 列表 (取第一行的 keys)."""
        if rows and isinstance(rows[0], dict):
            self._actual_schema = sorted(rows[0].keys())
        return self

    def with_schema(self, columns: list[str]) -> "FailReport":
        """直接传 column 列表 (不需要先从 row 取)."""
        self._actual_schema = sorted(columns)
        return self

    def with_sample(self, rows: list[Any] | None, n: int = 2) -> "FailReport":
        """前 N 行 sample (会限制 dict size 避免输出太长)."""
        if rows:
            self._sample_rows = rows[:n]
        return self

    def fuzzy_match_keys(self, expected: list[str], rows: list[dict] | None) -> "FailReport":
        """每个 expected key 找最接近的实际 column, 给"是不是想用 X" 提示."""
        if not rows or not isinstance(rows[0], dict):
            return self
        actual = list(rows[0].keys())
        for exp in expected:
            matches = difflib.get_close_matches(exp, actual, n=1, cutoff=0.3)
            if matches:
                similarity = difflib.SequenceMatcher(None, exp, matches[0]).ratio()
                self._fuzzy_hints.append(
                    f"'{exp}' → maybe '{matches[0]}' (similarity {similarity:.2f})"
                )
            else:
                self._fuzzy_hints.append(
                    f"'{exp}' → no close match in actual columns"
                )
        return self

    def add_hint(self, hint: str) -> "FailReport":
        self._extra_hints.append(hint)
        return self

    def format(self) -> str:
        """格式化成多行字符串, 适合 print / 写入 YAML note."""
        lines = [f"ERROR {self.what}"]
        lines.append(f"  Looked for: {self.looked_for}")
        if self._actual_schema is not None:
            lines.append(f"  Actual columns: {self._actual_schema}")
        if self._sample_rows is not None:
            lines.append(f"  Sample first {len(self._sample_rows)} rows:")
            for row in self._sample_rows:
                if isinstance(row, dict):
                    # 截断长值避免输出太大
                    compact = {k: (str(v)[:50] + "..." if isinstance(v, str) and len(v) > 50 else v)
                               for k, v in row.items()}
                    lines.append(f"    {json.dumps(compact, ensure_ascii=False, default=str)}")
                else:
                    lines.append(f"    {row}")
        if self._fuzzy_hints:
            lines.append(f"  Fuzzy match (期望 vs 实际):")
            for hint in self._fuzzy_hints:
                lines.append(f"    {hint}")
        if self._extra_hints:
            lines.append(f"  Hint:")
            for hint in self._extra_hints:
                lines.append(f"    {hint}")
        return "\n".join(lines)


def diff_dict_keys(expected: set | list, actual: set | list) -> dict:
    """返回 missing (期望有 actual 没) + extra (actual 有 expected 没)."""
    exp_set = set(expected)
    act_set = set(actual)
    return {
        "missing": sorted(exp_set - act_set),
        "extra": sorted(act_set - exp_set),
        "both": sorted(exp_set & act_set),
    }


def suggest_field_rename(expected_keys: list[str], actual_keys: list[str]) -> dict[str, str]:
    """对每个期望 key 找最接近的实际 key. Return {expected: suggested_actual}."""
    suggestions = {}
    for exp in expected_keys:
        matches = difflib.get_close_matches(exp, actual_keys, n=1, cutoff=0.3)
        if matches:
            suggestions[exp] = matches[0]
    return suggestions
