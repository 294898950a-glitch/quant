"""Hypothesizer (Layer 9) — proposes the next single edit to ``tunable_space.yaml``.

Role definition
---------------
The hypothesizer reads:

- the current writable surface (``editor.list_writable()``),
- recent runs (``memory.read_runs(last_n=5)``),
- the latest single-point :class:`judge.Diagnosis`,
- the global tried-direction index (``memory.search_history()``),

and emits **one** :class:`Hypothesis` describing what the orchestrator should
edit next. The actual write is performed downstream by ``editor.update_value``;
this module never touches disk for the strategy state, only optionally reads
``tunable_space.yaml`` (via :mod:`editor`) to validate proposed values.

Two paths
---------
1. **propose_via_llm** — calls DeepSeek (``deepseek-chat``) with a strict JSON
   contract. Output is rigorously validated against the writable surface,
   the per-item ``range``, the tried-direction memory and the format rules
   below. Up to ``max_llm_retries`` reprompts on invalid output.
2. **propose_via_rules** — deterministic fallback. Five priority rules,
   evaluated in order; the first match wins.

The top-level :func:`propose` orchestrates both paths and is contractually
**non-raising**: every conceivable internal exception is caught and degrades
to a rule-based proposal (or ``None`` if the rules also produce nothing).

Validation contract
-------------------
A :class:`Hypothesis` is accepted iff **all** hold:

1. ``item_path`` is in ``editor.list_writable()``.
2. ``new_value`` is inside that item's declared ``range`` (closed interval).
3. ``expected_direction`` is non-empty AND mentions a metric name AND a
   direction word (any of ``up / down / raise / lower / higher / lower /
   increase / decrease / arrow up / arrow down / 提高 / 降低 / 升 / 降``).
4. ``reason.strip()`` length ≥ 30.
5. ``confidence`` ∈ ``{"low", "medium", "high"}``.
6. The (item_path, direction-bucket, value-bucket) has not been tried with
   outcome ``"rejected"`` previously (consulted via
   :func:`memory.has_been_tried`).

Network safety
--------------
- ``DEEPSEEK_API_KEY`` not set → LLM path is skipped, rules run directly.
- Any ``httpx`` / network exception → caught, rules run.
- Tests MUST mock the LLM call site; this module never makes real network
  requests during pytest.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

from strategies.cb_redemption import editor as editor_mod
from strategies.cb_redemption import memory as memory_mod


# --------------------------------------------------------------------------- #
# Defaults / constants
# --------------------------------------------------------------------------- #

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT_S = 30.0

VALID_CONFIDENCE = {"low", "medium", "high"}
MIN_REASON_LEN = 30

# Direction-word vocabulary (any one suffices for validation).
DIRECTION_WORDS = (
    "up",
    "down",
    "raise",
    "lower",
    "higher",
    "increase",
    "decrease",
    "improve",
    "reduce",
    "shrink",
    "grow",
    "↑",
    "↓",
    "提高",
    "降低",
    "上升",
    "下降",
    "增加",
    "减少",
    "收紧",
    "放宽",
)

# Metric vocabulary surfaced in diagnosis / backtest dicts. Used to validate
# expected_direction has *some* metric anchor. ``excess_return`` is the
# framework's primary optimisation target — strategy total return minus the
# CB equal-weight index over the same dates.
METRIC_WORDS = (
    "sharpe",
    "is_sharpe",
    "oos_sharpe",
    "is_oos_gap",
    "winrate",
    "win_rate",
    "drawdown",
    "dd",
    "avg_return",
    "pnl",
    "return",
    "excess_return",
    "alpha",
    "gap",
    "stability",
    "trades",
    "胜率",
    "回撤",
    "收益",
    "超额",
    "夏普",
)


# --------------------------------------------------------------------------- #
# Hypothesis dataclass
# --------------------------------------------------------------------------- #


@dataclass
class Hypothesis:
    """A single proposed edit.

    Attributes
    ----------
    item_path : str
        Dotted path inside ``tunable_space.yaml``,
        e.g. ``"parameters.w_premium_ratio"``.
    new_value : float | int | bool | str
        The value the editor should write into ``.current``. Must be inside
        the declared ``range`` of that item.
    expected_direction : str
        Short claim of which metric should move which way after the edit
        — must include a metric word (e.g. ``oos_sharpe``) AND a
        direction word (e.g. ``up``). Example::

            "oos_sharpe up by ≥0.02"

    reason : str
        Free-form explanation, ≥ 30 chars after ``strip()``.
    confidence : str
        One of ``"low" | "medium" | "high"``.
    source : str
        ``"llm"`` if produced by :func:`propose_via_llm`, ``"rules"`` if
        by :func:`propose_via_rules`. Filled by :func:`propose`; tells the
        orchestrator how to weight / log the suggestion downstream.
    """

    item_path: str
    new_value: Any
    expected_direction: str
    reason: str
    confidence: str
    source: str = "rules"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _writable_index(writable_items: list[dict]) -> dict[str, dict]:
    """Map ``item_path`` → entry, for O(1) lookup during validation."""
    return {it["item_path"]: it for it in writable_items}


def _direction_bucket(old_value: Any, new_value: Any) -> str:
    """Coerce a delta into ``increase | decrease | set``.

    Mirrors :class:`memory.AttemptKey` semantics so we can ask
    :func:`memory.has_been_tried` with a key that matches what the
    orchestrator would later record.
    """
    try:
        if float(new_value) > float(old_value):
            return "increase"
        if float(new_value) < float(old_value):
            return "decrease"
        return "set"
    except (TypeError, ValueError):
        return "set"


def _has_direction_word(text: str) -> bool:
    low = text.lower()
    return any(w in low or w in text for w in DIRECTION_WORDS)


def _has_metric_word(text: str) -> bool:
    low = text.lower()
    return any(w in low or w in text for w in METRIC_WORDS)


def _validate_hypothesis(
    h: Hypothesis,
    writable_items: list[dict],
    tried_path: Path,
) -> tuple[bool, str]:
    """Check ``h`` against all six contract rules. Returns
    ``(ok, reason_if_rejected)``.

    The validation is intentionally pure — no I/O beyond the
    ``tried_directions.jsonl`` lookup driven by ``tried_path``.
    """
    idx = _writable_index(writable_items)

    # Rule 1: item_path must exist.
    item = idx.get(h.item_path)
    if item is None:
        return False, f"unknown item_path: {h.item_path!r}"

    # Rule 2: value within range.
    rng = item.get("range")
    if not (isinstance(rng, list) and len(rng) == 2):
        return False, f"item {h.item_path} has malformed range: {rng!r}"
    lo, hi = rng[0], rng[1]
    try:
        v = float(h.new_value)
    except (TypeError, ValueError):
        return False, f"new_value {h.new_value!r} not numeric"
    if not (lo <= v <= hi):
        return False, f"new_value {v} out of range [{lo}, {hi}]"

    # Rule 3: expected_direction non-empty + metric + direction.
    ed = (h.expected_direction or "").strip()
    if not ed:
        return False, "expected_direction empty"
    if not _has_direction_word(ed):
        return False, f"expected_direction missing direction word: {ed!r}"
    if not _has_metric_word(ed):
        return False, f"expected_direction missing metric word: {ed!r}"

    # Rule 4: reason length.
    if len(h.reason.strip()) < MIN_REASON_LEN:
        return False, f"reason too short ({len(h.reason.strip())} < {MIN_REASON_LEN})"

    # Rule 5: confidence enum.
    if h.confidence not in VALID_CONFIDENCE:
        return False, f"confidence {h.confidence!r} not in {VALID_CONFIDENCE}"

    # Rule 6: not previously tried with outcome=rejected.
    old_value = item.get("current")
    direction = _direction_bucket(old_value, h.new_value)
    key = memory_mod.AttemptKey.from_value(
        item_path=h.item_path,
        direction=direction,
        new_value=h.new_value,
    )
    try:
        prior = memory_mod.has_been_tried(key, path=tried_path)
    except Exception:
        # Memory unreadable — treat as no prior; never block hypothesis on this.
        prior = []
    if any(r.get("outcome") == "rejected" for r in prior):
        return False, (
            f"direction already tried & rejected: "
            f"{key.to_str()}"
        )

    return True, ""


# --------------------------------------------------------------------------- #
# Rule-based fallback
# --------------------------------------------------------------------------- #


def _hypothesis_from_rule(
    item: dict,
    new_value: Any,
    rule_no: int,
    description: str,
) -> Hypothesis:
    """Construct a rule-sourced :class:`Hypothesis` with stock metadata."""
    reason = f"规则 {rule_no} 触发：{description}".strip()
    # Reason must be >= 30 chars after strip; guard with padding.
    if len(reason) < MIN_REASON_LEN:
        reason = (reason + " " + "依据：见 hypothesizer.propose_via_rules").strip()
    return Hypothesis(
        item_path=item["item_path"],
        new_value=new_value,
        expected_direction="oos_sharpe up by ≥0.02",
        reason=reason,
        confidence="low",
        source="rules",
    )


def _move_toward_zero(current: float, fraction: float) -> float:
    """Move ``current`` ``fraction`` of the way toward 0."""
    return round(current - current * fraction, 4)


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def propose_via_rules(
    writable_items: list[dict],
    diagnosis: dict,
    tried_path: Path,
) -> Hypothesis | None:
    """Deterministic fallback. See module docstring for the five priorities.

    Returns the first rule-generated, validation-passing hypothesis, or
    ``None`` if no rule produces a fresh edit.
    """
    idx = _writable_index(writable_items)

    def _try_emit(h: Hypothesis) -> Hypothesis | None:
        """Validate and gate against tried-history; return h or None."""
        ok, _why = _validate_hypothesis(h, writable_items, tried_path)
        return h if ok else None

    # Rule 1: kill weak factors — halve the corresponding parameter weight
    # toward zero.
    weak = (diagnosis or {}).get("weak_factors") or []
    for wf in weak:
        # weak factor name is e.g. "premium_ratio"; corresponding param is
        # "parameters.w_premium_ratio".
        guess = f"parameters.w_{wf}"
        item = idx.get(guess)
        if item is None:
            continue
        cur = item.get("current")
        if not isinstance(cur, (int, float)):
            continue
        new = _move_toward_zero(float(cur), 0.5)
        lo, hi = item["range"]
        new = _clip(new, lo, hi)
        h = _hypothesis_from_rule(
            item,
            new,
            rule_no=1,
            description=(
                f"弱因子 {wf} 当前权重 {cur} → 朝 0 收缩 50% 至 {new}, "
                f"目标 oos_sharpe 提升"
            ),
        )
        out = _try_emit(h)
        if out is not None:
            return out

    # Rule 2: |is_oos_gap_sharpe| > 0.1 → shrink the largest |w| parameter
    # by 10% toward 0.
    gap = (diagnosis or {}).get("is_oos_gap_sharpe")
    if isinstance(gap, (int, float)) and abs(gap) > 0.1:
        params = [
            it
            for it in writable_items
            if it["item_path"].startswith("parameters.")
            and isinstance(it.get("current"), (int, float))
        ]
        params.sort(key=lambda it: -abs(float(it["current"])))
        for item in params:
            cur = float(item["current"])
            new = _move_toward_zero(cur, 0.1)
            lo, hi = item["range"]
            new = _clip(new, lo, hi)
            h = _hypothesis_from_rule(
                item,
                new,
                rule_no=2,
                description=(
                    f"|is_oos_gap_sharpe|={abs(gap):.3f}>0.1, 收缩"
                    f" {item['item_path']} 权重 10% ({cur} → {new}) 抑制过拟合"
                ),
            )
            out = _try_emit(h)
            if out is not None:
                return out

    # Rule 3: any quarter winrate < 30 → raise thresholds.action by 0.05.
    by_q = (diagnosis or {}).get("by_quarter") or []
    if any(
        isinstance(q.get("winrate"), (int, float)) and float(q["winrate"]) < 30.0
        for q in by_q
    ):
        item = idx.get("thresholds.action")
        if item is not None and isinstance(item.get("current"), (int, float)):
            cur = float(item["current"])
            new = round(cur + 0.05, 4)
            lo, hi = item["range"]
            new = _clip(new, lo, hi)
            if new != cur:
                h = _hypothesis_from_rule(
                    item,
                    new,
                    rule_no=3,
                    description=(
                        f"存在季度 winrate<30%, 把 thresholds.action {cur} → {new} "
                        f"提高入场门槛, 期望 oos_sharpe 上升"
                    ),
                )
                out = _try_emit(h)
                if out is not None:
                    return out

    # Rule 4: any year avg_return < 0 → raise rules.stop_loss_pct by 0.02
    # (i.e. tighten — stop_loss_pct is negative, so "raise" means closer to 0).
    by_y = (diagnosis or {}).get("by_year") or []
    if any(
        isinstance(y.get("avg_return"), (int, float)) and float(y["avg_return"]) < 0.0
        for y in by_y
    ):
        item = idx.get("rules.stop_loss_pct")
        if item is not None and isinstance(item.get("current"), (int, float)):
            cur = float(item["current"])
            new = round(cur + 0.02, 4)
            lo, hi = item["range"]
            new = _clip(new, lo, hi)
            if new != cur:
                h = _hypothesis_from_rule(
                    item,
                    new,
                    rule_no=4,
                    description=(
                        f"存在年份 avg_return<0, 把 rules.stop_loss_pct"
                        f" {cur} → {new} 收紧, 期望 oos_sharpe 上升"
                    ),
                )
                out = _try_emit(h)
                if out is not None:
                    return out

    # Rule 5: pick any writable item with no recorded attempts; nudge value
    # toward range mid-point.
    for item in writable_items:
        cur = item.get("current")
        if not isinstance(cur, (int, float)):
            continue
        lo, hi = item["range"]
        mid = (lo + hi) / 2.0
        # Move 10% of the way to the midpoint, away from current.
        new = round(cur + (mid - cur) * 0.1, 4)
        new = _clip(new, lo, hi)
        if new == cur:
            continue
        # Skip if any attempt with this exact bucket already on file (any outcome).
        direction = _direction_bucket(cur, new)
        key = memory_mod.AttemptKey.from_value(
            item_path=item["item_path"],
            direction=direction,
            new_value=new,
        )
        try:
            prior = memory_mod.has_been_tried(key, path=tried_path)
        except Exception:
            prior = []
        if prior:
            continue
        h = _hypothesis_from_rule(
            item,
            new,
            rule_no=5,
            description=(
                f"无规则触发, 在未尝试方向上探索 {item['item_path']}"
                f" {cur} → {new} (向 range 中点 {mid:.4f} 靠拢 10%)"
            ),
        )
        out = _try_emit(h)
        if out is not None:
            return out

    return None


# --------------------------------------------------------------------------- #
# LLM client wrapper
# --------------------------------------------------------------------------- #


def _build_system_prompt() -> str:
    """System prompt: role + strict output contract."""
    return (
        "你是量化策略自循环框架的『出主意者』(hypothesizer)。你的唯一任务是看历史回测、最新诊断和已尝试方向, "
        "提出下一步要修改 tunable_space.yaml 中的哪一项数值, 以期 **excess_return** (超额收益) 改善。"
        "\n\n"
        "## 核心目标 (重要)\n"
        "你的核心目标是最大化 **excess_return** = 策略 OOS 总收益 - 同期可转债等权指数收益, "
        "**不是** sharpe (得分) 或单纯 total_return.\n"
        "\n"
        "excess_return 反映你真正比『持有 CB 不动』多赚的部分 — 这是真 alpha. "
        "sharpe 高但 excess_return 接近 0 说明只是『不动』而已, 没有任何超额价值.\n"
        "\n"
        "历史数据显示该策略空间里, 激进版本 (单只仓位大、放宽信号、持仓多) 反而能吃到更多 alpha; "
        "过度保守版本会把 alpha 缩到很小. 不要默认『减仓 = 风险低 = 好』, "
        "请基于实际 excess_return 变化决定方向.\n"
        "\n"
        "## 输出契约\n"
        "严格输出 JSON 对象 (response_format=json_object), 必须含且仅含以下键:\n"
        '  - item_path (string): 形如 "parameters.w_premium_ratio", 必须在用户传入的 writable_items 列表中.\n'
        "  - new_value (number/bool/string): 必须落在该 item_path 的 range 闭区间内.\n"
        '  - expected_direction (string): 必须同时含指标名 (excess_return/sharpe/oos_sharpe/winrate/drawdown 等) 和方向词 '
        '(up/down/raise/lower/提高/降低/↑/↓ 等), 优先用 excess_return, 例: "excess_return up by >=0.02".\n'
        "  - reason (string): >=30 字符, 解释为什么这么改, 引用诊断里的具体数字 (重点引 excess_return).\n"
        '  - confidence (string): "low" | "medium" | "high".\n'
        "\n"
        "禁止: 输出多个候选; 输出 markdown / 代码块 / 解释; 修改 writable_items 之外的项; "
        "提议在 tried_directions 中已 outcome=rejected 的方向; new_value 越界."
    )


def _summarise_run(rec: Any) -> dict:
    """Strip a RunRecord-shaped dict to fields useful for prompting.

    Includes ``excess_return`` (策略 - CB 等权指数, 真 alpha 信号) alongside
    sharpe / total_return so the LLM can compare both. ``excess_return`` is
    the framework's primary optimisation target — sharpe is shown for
    context but should not drive decisions.
    """
    if hasattr(rec, "to_dict"):
        rec = rec.to_dict()
    if not isinstance(rec, dict):
        return {}
    bt = rec.get("backtest") or {}
    is_m = bt.get("is_metrics") or {}
    oos_m = bt.get("oos_metrics") or {}
    all_m = bt.get("all_metrics") or {}
    return {
        "iteration": rec.get("iteration"),
        # 重点关注 excess_return — 真 alpha
        "oos_excess_return": oos_m.get("excess_return"),
        "is_excess_return": is_m.get("excess_return"),
        "all_excess_return": all_m.get("excess_return"),
        # 总收益 (含基准在内的绝对收益)
        "oos_total_return": oos_m.get("total_return"),
        "is_total_return": is_m.get("total_return"),
        # sharpe (上下文参考, 不是优化目标)
        "is_sharpe": is_m.get("sharpe"),
        "oos_sharpe": oos_m.get("sharpe"),
        "is_winrate": is_m.get("win_rate"),
        "oos_winrate": oos_m.get("win_rate"),
    }


def _summarise_tried(tried_directions: list[dict]) -> list[dict]:
    """Compress to (item_path, direction, bucket_value, outcome) only."""
    out: list[dict] = []
    for t in tried_directions or []:
        out.append(
            {
                "item_path": t.get("item_path"),
                "direction": t.get("direction"),
                "bucket_value": t.get("bucket_value"),
                "outcome": t.get("outcome"),
            }
        )
    return out


def _format_pool_stats_section(pool_stats: dict | None) -> str:
    """Render the raw-statistic block appended to the user message.

    Pure formatting: numbers in, Chinese lines out. **Must not** introduce
    market-state labels (bull/bear/ranging/volatile/dead, 牛/熊/震荡)
    here or in the surrounding instructions — the framework's stated
    contract is that the LLM is the one that judges, not the prompt.

    Returns an empty string if ``pool_stats`` is None or empty so the
    user message stays clean for cb_redemption-style strategies that
    don't supply stats.
    """
    if not pool_stats:
        return ""
    lines = [
        f"累计涨跌: {float(pool_stats.get('trend_pct', 0.0)):+.1%}",
        f"日斜率: {float(pool_stats.get('slope_per_day', 0.0)):+.4f}%",
        f"日波动率: {float(pool_stats.get('vol_daily', 0.0)):.2%}",
        f"区间宽度: {float(pool_stats.get('range_pct', 0.0)):.1%}",
        f"样本数: {int(pool_stats.get('sample_n', 0))}",
    ]
    return (
        "\n\n## 当前数据切片的统计描述\n"
        + "\n".join(f"- {l}" for l in lines)
        + "\n\n请从这些原始数字判断当前市况, 据此给参数建议. "
        + "不要默认任何标签 — 这些数字本身就是事实."
    )


def _format_recent_runs_focus(summarised: list[dict]) -> str:
    """Render last-N-runs in a human-readable list that visually emphasises
    excess_return (真 alpha). The same data also lives in the JSON payload;
    this block is purely cognitive scaffolding for the LLM.
    """
    if not summarised:
        return ""
    lines: list[str] = []
    for r in summarised[-5:]:
        it = r.get("iteration")
        sh = r.get("oos_sharpe")
        tr = r.get("oos_total_return")
        ex = r.get("oos_excess_return")
        # 渲染: 缺失字段显示为 ?
        sh_s = f"{float(sh):.3f}" if isinstance(sh, (int, float)) else "?"
        tr_s = (
            f"{float(tr) * 100:+.2f}%" if isinstance(tr, (int, float)) else "?"
        )
        ex_s = (
            f"{float(ex) * 100:+.2f}%" if isinstance(ex, (int, float)) else "?"
        )
        lines.append(
            f"  iter {it}: sharpe={sh_s}, total_return={tr_s}, "
            f"excess_return={ex_s}  ← 重点关注 excess_return"
        )
    return (
        "\n\n## 最近 N 轮回顾 (excess_return = 真 alpha)\n"
        + "\n".join(lines)
        + "\n\n注: sharpe 高但 excess_return 接近 0 = 没跑赢基准, 不是好结果. "
        + "目标是 excess_return 持续上升."
    )


def _build_user_prompt(
    writable_items: list[dict],
    recent_runs: list[Any],
    diagnosis: dict,
    tried_directions: list[dict],
    prev_error: str | None,
    pool_stats: dict | None = None,
) -> str:
    """Pack all inputs into one prompt body.

    The optional ``pool_stats`` argument carries a label-free numeric
    description of the currently-attached holdout pool; if supplied it
    is appended as a separate human-readable section after the JSON
    payload so the LLM sees both the raw machine-friendly facts and a
    quick-read summary.
    """
    summarised_runs = [_summarise_run(r) for r in (recent_runs or [])]
    payload = {
        "writable_items": writable_items,
        "recent_runs": summarised_runs,
        "diagnosis": diagnosis or {},
        "tried_directions": _summarise_tried(tried_directions),
    }
    body = json.dumps(payload, ensure_ascii=False)
    pool_block = _format_pool_stats_section(pool_stats)
    runs_block = _format_recent_runs_focus(summarised_runs)
    if prev_error:
        return (
            f"上一轮你的输出被拒绝, 原因: {prev_error}\n"
            f"请重出一份合规 JSON。输入数据如下:\n{body}{pool_block}{runs_block}"
        )
    return f"输入数据 (JSON):\n{body}{pool_block}{runs_block}"


def _default_llm_call(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = DEEPSEEK_TIMEOUT_S,
) -> str:
    """Call DeepSeek via httpx; returns the raw assistant message text.

    Raises whatever httpx raises — caller is expected to catch.
    """
    import httpx  # local import keeps test isolation easy

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": DEEPSEEK_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(DEEPSEEK_ENDPOINT, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
    # Defensive parse — both "choices[0].message.content" and odd shapes.
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("LLM response has no choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise ValueError("LLM response message.content not string")
    return content


def _parse_llm_json(text: str) -> dict:
    """Parse the JSON object the LLM returned. Raises ValueError on failure."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM output not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"LLM output not a JSON object (got {type(obj).__name__})")
    return obj


def _build_hypothesis_from_obj(obj: dict) -> Hypothesis:
    """Materialise a :class:`Hypothesis` from a parsed JSON dict.

    Missing fields raise KeyError → caught upstream as a validation error.
    """
    return Hypothesis(
        item_path=str(obj["item_path"]),
        new_value=obj["new_value"],
        expected_direction=str(obj["expected_direction"]),
        reason=str(obj["reason"]),
        confidence=str(obj["confidence"]),
        source="llm",
    )


def propose_via_llm(
    writable_items: list[dict],
    recent_runs: list[Any],
    diagnosis: dict,
    tried_directions: list[dict],
    tried_path: Path,
    llm_call: Callable[[str, str, str], str] | None = None,
    max_retries: int = 2,
    pool_stats: dict | None = None,
) -> Hypothesis | None:
    """Try to extract a valid hypothesis from the LLM.

    Parameters
    ----------
    llm_call : callable, optional
        Injection point for the network call. Signature:
        ``(api_key, system_prompt, user_prompt) -> str``. Defaults to
        :func:`_default_llm_call`. Tests pass a mock here.
    max_retries : int
        Number of *additional* attempts after the first call (so
        ``max_retries=2`` → up to 3 calls in total).
    pool_stats : dict, optional
        Raw numerical description of the currently-attached holdout
        pool (see :mod:`pool_stats`). When supplied, the prompt
        includes a small Chinese summary so the LLM can use the
        numbers for its market read. **No labels** — only digits.

    Returns
    -------
    Hypothesis | None
        ``None`` if the LLM is unreachable, has no API key, or fails all
        attempts.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    if llm_call is None:
        llm_call = _default_llm_call

    system_prompt = _build_system_prompt()
    last_error: str | None = None

    for attempt in range(max_retries + 1):
        user_prompt = _build_user_prompt(
            writable_items,
            recent_runs,
            diagnosis,
            tried_directions,
            last_error,
            pool_stats=pool_stats,
        )
        try:
            raw = llm_call(api_key, system_prompt, user_prompt)
        except Exception as exc:  # network / http / json — all bail to rules
            last_error = f"network/llm error: {type(exc).__name__}: {exc}"
            # Network errors don't get retried — return None and let propose
            # decide what to do (it falls back to rules).
            return None

        try:
            obj = _parse_llm_json(raw)
            h = _build_hypothesis_from_obj(obj)
        except (KeyError, ValueError, TypeError) as exc:
            last_error = f"parse error: {exc}"
            continue

        ok, why = _validate_hypothesis(h, writable_items, tried_path)
        if ok:
            return h
        last_error = why
        # loop and retry

    return None


# --------------------------------------------------------------------------- #
# Top-level entry
# --------------------------------------------------------------------------- #


def propose(
    writable_items: list[dict],
    recent_runs: list[Any],
    diagnosis: dict,
    tried_directions: list[dict],
    llm_client: Callable[[str, str, str], str] | None = None,
    space_path: Path = editor_mod.DEFAULT_SPACE_FILE,
    runs_path: Path = memory_mod.DEFAULT_RUNS_FILE,
    tried_path: Path = memory_mod.DEFAULT_ATTEMPTS_FILE,
    max_llm_retries: int = 2,
    pool_stats: dict | None = None,
) -> Hypothesis | None:
    """Top-level hypothesizer entry. Never raises.

    Tries the LLM path first; on any failure (no API key, network,
    bad JSON, validation, etc.) falls back to the deterministic rule set.
    Returns ``None`` only if rules also produce nothing.

    Parameters
    ----------
    writable_items : list[dict]
        Result of :func:`editor.list_writable`.
    recent_runs : list[dict | RunRecord]
        Result of :func:`memory.read_runs(last_n=5)` (serialised or not).
    diagnosis : dict
        ``judge.Diagnosis.to_dict()`` for the latest single backtest.
    tried_directions : list[dict]
        Result of :func:`memory.search_history()` over the relevant horizon.
    llm_client : callable, optional
        Injected LLM call site for tests. ``(api_key, system, user) -> str``.
    space_path, runs_path, tried_path : Path
        Pass-through overrides for default file locations. Only
        ``tried_path`` is consulted internally (for de-dup); the others
        are accepted to keep the signature symmetric with neighbouring
        layers and reserved for future use.
    max_llm_retries : int
        Forwarded to :func:`propose_via_llm`.
    pool_stats : dict, optional
        Raw numbers describing the current holdout pool (see
        :mod:`pool_stats`). Forwarded to the LLM prompt. ``None`` (the
        default) is the cb_redemption case — that strategy does not
        wire a price loader, so the prompt is unchanged.
    """
    # Path A — LLM. Fully wrapped in try/except: contract says we never raise.
    try:
        h = propose_via_llm(
            writable_items=writable_items,
            recent_runs=recent_runs,
            diagnosis=diagnosis,
            tried_directions=tried_directions,
            tried_path=tried_path,
            llm_call=llm_client,
            max_retries=max_llm_retries,
            pool_stats=pool_stats,
        )
        if h is not None:
            return h
    except Exception:
        # Absolute belt-and-braces — propose() must not raise.
        pass

    # Path B — rules.
    try:
        return propose_via_rules(
            writable_items=writable_items,
            diagnosis=diagnosis or {},
            tried_path=tried_path,
        )
    except Exception:
        return None


__all__ = [
    "Hypothesis",
    "propose",
    "propose_via_llm",
    "propose_via_rules",
    "DEEPSEEK_ENDPOINT",
    "DEEPSEEK_MODEL",
]
