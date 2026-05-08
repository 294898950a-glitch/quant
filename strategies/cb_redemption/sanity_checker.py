"""Sanity Checker (Layer 8.5) — input-validity gate before each verifier run.

Role definition
---------------
The 8th role in the self-loop framework. Sits in front of the verifier, runs
*before* a backtest is launched, and refuses to spend compute on parameter
combinations that are mechanically incompatible with the current data shape
(e.g. ``range_window=200`` with a 101-day pool — engine never reaches steady
state) or internally contradictory (e.g. ``trend_short_window >=
trend_long_window``).

Why this is a separate role
---------------------------
- The **judge** sees only one backtest's metrics; it can flag a poor run but
  has no way to tell whether the run was *meaningless* (params + data shape
  ill-formed) vs *bad* (good shape, wrong direction).
- The **auditor** looks at trajectories; by the time an obviously-broken
  parameter set has been backtested it's already polluting trend statistics,
  and aborting after the fact wastes a holdout iteration.
- The **hypothesizer** picks the next edit; it doesn't double-check that the
  *current* state is sane before that edit lands.

Sanity checker fills the gap: cheap, fast, runs unconditionally.

Two-layer design
----------------
1. **Hard rules** (`check_hard_rules`) — pure, deterministic, zero-cost.
   Runs every iteration. Catches the specific shapes already seen to fail
   in the wild (range_window > pool_size; trend_short >= trend_long; etc.).
2. **LLM evaluation** (`check_with_llm`) — DeepSeek call with strict JSON
   contract. Event-driven: only invoked at startup, after auto-shift, after
   pool rotation, after the auditor flags ``data_mining``, and once every
   20 iterations as a heartbeat.

Top-level :func:`check` orchestrates both layers, returning a single
:class:`SanityReport`. ``verdict == "fatal"`` means *do not run the verifier
this iteration* — the orchestrator should auto-shift instead.

Failure mode
------------
The contract is **non-raising**: hard-rule bugs degrade to *no issue* (LLM
catches it), LLM failures degrade to hard-rule-only output. The loop is
never blocked by this layer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# DeepSeek defaults
# --------------------------------------------------------------------------- #

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT_S = 30.0

VALID_VERDICTS = ("ok", "warn", "fatal")
VALID_SEVERITIES = ("warn", "fatal")

# 简化映射:hard-rules 模式下没有 LLM 评分,用 verdict 倒推一个数值
_RULES_SCORE_MAP = {"ok": 10.0, "warn": 4.0, "fatal": 0.0}

# 周期补漏 — every N iters force an LLM check even without an event trigger.
LLM_PERIODIC_GUARD_ITERS = 20


# --------------------------------------------------------------------------- #
# Public dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class SanityReport:
    """Result of a single sanity pass.

    Attributes
    ----------
    verdict : str
        One of ``"ok" | "warn" | "fatal"``. ``fatal`` means the orchestrator
        MUST refuse to run the verifier this iteration.
    score : float
        0..10 quality score. In rules-only mode this is a coarse map
        (fatal=0, warn=4, ok=10). LLM-mode uses the LLM's own number.
    issues : list[dict]
        Each entry: ``{"severity": "warn"|"fatal", "code": str, "message": str}``.
    advice : str | None
        Optional free-form Chinese guidance from the LLM. ``None`` in
        rules-only mode (we don't synthesise advice — the hypothesizer is
        the one that proposes concrete edits).
    summary : str
        One-sentence Chinese summary, always populated.
    layer : str
        ``"rules"`` or ``"rules+llm"`` — provenance tag.
    """

    verdict: str
    score: float
    issues: list[dict] = field(default_factory=list)
    advice: str | None = None
    summary: str = ""
    layer: str = "rules"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "score": float(self.score),
            "issues": list(self.issues),
            "advice": self.advice,
            "summary": self.summary,
            "layer": self.layer,
        }


def _make_issue(severity: str, code: str, message: str) -> dict:
    return {"severity": severity, "code": code, "message": message}


# --------------------------------------------------------------------------- #
# Layer 1 — hard rules
# --------------------------------------------------------------------------- #


def _writable_index(writable_items: list[dict]) -> dict[str, dict]:
    """Map ``item_path`` → entry, for O(1) lookup."""
    return {it["item_path"]: it for it in (writable_items or [])}


def _get_current(idx: dict[str, dict], path: str) -> Any:
    """Return ``current`` for ``path`` if present and a number, else None."""
    item = idx.get(path)
    if item is None:
        return None
    cur = item.get("current")
    if isinstance(cur, bool):
        # bool is a numeric subclass but we want it as a flag, not a value
        return int(cur)
    if isinstance(cur, (int, float)):
        return cur
    return None


def check_hard_rules(
    writable_items: list[dict],
    pool_size: int | None,
) -> list[dict]:
    """Run zero-cost hard rules. **Never raises** — any internal exception
    degrades to "no issue".

    Returns
    -------
    list[dict]
        Each issue is ``{"severity": "fatal"|"warn", "code": str,
        "message": str}``. Empty list = nothing flagged.
    """
    try:
        return _check_hard_rules_inner(writable_items, pool_size)
    except Exception:
        # Defensive: never let a hard-rule bug block the loop. LLM layer
        # (or downstream judge/auditor) will catch real problems.
        return []


def _check_hard_rules_inner(
    writable_items: list[dict],
    pool_size: int | None,
) -> list[dict]:
    issues: list[dict] = []
    idx = _writable_index(writable_items)

    # Rule 0 (universal): any ``current`` outside its declared ``range`` is
    # fatal. Editor *should* prevent this from ever happening, but a paranoid
    # double-check costs nothing and catches yaml hand-edits.
    for item in writable_items or []:
        cur = item.get("current")
        rng = item.get("range")
        path = item.get("item_path", "?")
        if not (isinstance(rng, list) and len(rng) == 2):
            continue
        lo, hi = rng[0], rng[1]
        if isinstance(cur, bool):
            cur_num = float(int(cur))
        elif isinstance(cur, (int, float)):
            cur_num = float(cur)
        else:
            continue  # non-numeric current not validated here
        try:
            if cur_num < float(lo) or cur_num > float(hi):
                issues.append(
                    _make_issue(
                        "fatal",
                        "current_out_of_range",
                        f"{path} current={cur} 越出 range=[{lo}, {hi}]",
                    )
                )
        except (TypeError, ValueError):
            continue

    # Rule 1 (grid only): range_window > pool_size → fatal.
    rw = _get_current(idx, "parameters.range_window")
    if rw is not None and pool_size is not None:
        try:
            if int(rw) > int(pool_size):
                issues.append(
                    _make_issue(
                        "fatal",
                        "range_window_exceeds_pool",
                        f"range_window={rw} > pool_size={pool_size}; 引擎进不了稳态, "
                        f"无法形成滚动区间, 此次回测无意义",
                    )
                )
        except (TypeError, ValueError):
            pass

    # Rule 2 (grid only): trend_short_window >= trend_long_window → fatal.
    tsw = _get_current(idx, "parameters.trend_short_window")
    tlw = _get_current(idx, "parameters.trend_long_window")
    if tsw is not None and tlw is not None:
        try:
            if float(tsw) >= float(tlw):
                issues.append(
                    _make_issue(
                        "fatal",
                        "trend_short_not_less_than_long",
                        f"trend_short_window={tsw} >= trend_long_window={tlw}; "
                        f"短均线必须严格短于长均线",
                    )
                )
        except (TypeError, ValueError):
            pass

    # Rule 3 (grid only): grid_count * position_per_grid > 1.5 → warn.
    gc = _get_current(idx, "parameters.grid_count")
    ppg = _get_current(idx, "parameters.position_per_grid")
    if gc is not None and ppg is not None:
        try:
            total_pos = float(gc) * float(ppg)
            if total_pos > 1.5:
                issues.append(
                    _make_issue(
                        "warn",
                        "grid_total_position_exceeds_full",
                        f"grid_count×position_per_grid={total_pos:.3f}>1.5; "
                        f"满仓不可能(资金算不过来)",
                    )
                )
        except (TypeError, ValueError):
            pass

    # Rule 4 (grid only): vol_atr_window > range_window → warn.
    vaw = _get_current(idx, "rules.vol_atr_window")
    if vaw is not None and rw is not None:
        try:
            if float(vaw) > float(rw):
                issues.append(
                    _make_issue(
                        "warn",
                        "vol_atr_window_exceeds_range_window",
                        f"vol_atr_window={vaw} > range_window={rw}; "
                        f"波动窗口比区间窗口还长, 不合常理",
                    )
                )
        except (TypeError, ValueError):
            pass

    return issues


# --------------------------------------------------------------------------- #
# Layer 2 — LLM
# --------------------------------------------------------------------------- #


def _build_system_prompt() -> str:
    """LLM system prompt: role + strict JSON contract."""
    return (
        "你是量化策略自循环框架的『体检员』(sanity_checker)。你的唯一任务是看当前可调"
        "参数 + 当前数据形状 + 最近回测轨迹, 判断这组参数在跑回测之前是否合理、是否会"
        "得到有意义的结果。"
        "\n\n"
        "评估 5 个角度: \n"
        "  1. 参数内部一致性 — 短均线 < 长均线? 网格 × 单格 ≤ 1? 参数互相冲突?\n"
        "  2. 参数 vs 数据兼容 — 回看窗口 ≤ pool_size? 数据量足够形成稳态?\n"
        "  3. 策略逻辑是否说得通 — 网格策略上单边趋势 + filter 全关 = 逻辑不合理.\n"
        "  4. 是否卡在局部最小 — 参数贴范围边界, 反复在同一数字附近试.\n"
        "  5. 任何老手一眼能看出的反常 — 数值离谱, 组合明显错位.\n"
        "\n"
        "严格输出 JSON 对象 (response_format=json_object), 必须含且仅含以下键:\n"
        '  - verdict (string): "ok" | "warn" | "fatal".\n'
        "  - score (number): 0..10 整体合理性评分.\n"
        "  - issues (list): 每条形如 "
        '{"severity": "warn"|"fatal", "code": "snake_case_code", "message": "中文描述"}.\n'
        "  - advice (string): 给方向 (verdict != ok 时必填; ok 时可空字符串). "
        "只描述方向, 不直接给具体改值 — 给值是出主意者的事.\n"
        '  - summary (string): 一句话中文总评, ≥10 字.\n'
        "\n"
        "禁止:\n"
        "  - 输出多个候选方案, 输出 markdown 或代码块.\n"
        "  - 直接给出具体的参数新值 (那是 hypothesizer 的职责).\n"
        "  - 调用任何外部工具 / 网络资源, 你只看用户给的输入.\n"
    )


def _summarise_run(rec: Any) -> dict:
    """Strip a RunRecord-shaped dict for LLM consumption."""
    if hasattr(rec, "to_dict"):
        rec = rec.to_dict()
    if not isinstance(rec, dict):
        return {}
    bt = rec.get("backtest") or {}
    audit = rec.get("audit") or {}
    return {
        "iteration": rec.get("iteration"),
        "params": (rec.get("params") or {}).get("weights"),
        "oos_sharpe": ((bt.get("oos_metrics") or {}).get("sharpe")),
        "oos_trades": (
            (bt.get("oos_metrics") or {}).get("total_trades")
            or (bt.get("oos_metrics") or {}).get("trades")
        ),
        "verdict": audit.get("verdict") if isinstance(audit, dict) else None,
    }


def _build_user_prompt(
    writable_items: list[dict],
    pool_size: int | None,
    recent_runs: list[Any] | None,
    diagnosis: dict | None,
    strategy_name: str,
    asset_hint: str | None,
    prev_error: str | None,
) -> str:
    """Pack inputs into one user-message body."""
    payload = {
        "strategy_name": strategy_name,
        "asset_hint": asset_hint or "",
        "writable_items": writable_items or [],
        "pool_size": pool_size,
        "recent_runs": [_summarise_run(r) for r in (recent_runs or [])][-5:],
        "latest_diagnosis": diagnosis or {},
    }
    body = json.dumps(payload, ensure_ascii=False)
    if prev_error:
        return (
            f"上一轮你的输出被拒绝, 原因: {prev_error}\n"
            f"请重出一份合规 JSON。输入数据如下:\n{body}"
        )
    return f"输入数据 (JSON):\n{body}"


def _default_llm_call(
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = DEEPSEEK_TIMEOUT_S,
) -> str:
    """Call DeepSeek via httpx; returns the raw assistant message text.

    Raises whatever httpx raises — caller catches.
    """
    import httpx

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
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("LLM response has no choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise ValueError("LLM response message.content not string")
    return content


def _validate_llm_obj(obj: dict) -> tuple[bool, str]:
    """Strict structural validation of the LLM JSON object.

    Returns ``(ok, reason_if_rejected)``.
    """
    if not isinstance(obj, dict):
        return False, "not an object"

    verdict = obj.get("verdict")
    if verdict not in VALID_VERDICTS:
        return False, f"verdict {verdict!r} not in {VALID_VERDICTS}"

    score = obj.get("score")
    try:
        s = float(score)
    except (TypeError, ValueError):
        return False, f"score {score!r} not numeric"
    if not (0.0 <= s <= 10.0):
        return False, f"score {s} out of [0, 10]"

    issues = obj.get("issues")
    if not isinstance(issues, list):
        return False, "issues must be a list"
    for i, iss in enumerate(issues):
        if not isinstance(iss, dict):
            return False, f"issues[{i}] not an object"
        sev = iss.get("severity")
        if sev not in VALID_SEVERITIES:
            return False, f"issues[{i}].severity {sev!r} not in {VALID_SEVERITIES}"
        msg = iss.get("message")
        if not isinstance(msg, str) or not msg.strip():
            return False, f"issues[{i}].message empty or not string"

    advice = obj.get("advice", "")
    if verdict != "ok":
        if not isinstance(advice, str) or not advice.strip():
            return False, "advice empty when verdict != ok"
    else:
        # ok 时 advice 可空 — 只要键存在且是字符串/None 就行
        if advice is not None and not isinstance(advice, str):
            return False, "advice must be string or null"

    summary = obj.get("summary", "")
    if not isinstance(summary, str) or len(summary.strip()) < 10:
        return False, "summary missing or shorter than 10 chars"

    return True, ""


def check_with_llm(
    writable_items: list[dict],
    pool_size: int | None,
    recent_runs: list[Any] | None,
    diagnosis: dict | None,
    strategy_name: str,
    asset_hint: str | None = None,
    llm_client: Callable[[str, str, str], str] | None = None,
    max_retries: int = 2,
) -> SanityReport | None:
    """Ask the LLM whether the current state is sane.

    Parameters
    ----------
    llm_client : callable, optional
        Injection point ``(api_key, system_prompt, user_prompt) -> str``.
        Tests pass a mock here; production uses :func:`_default_llm_call`.
    max_retries : int
        Additional attempts after the first call. ``2`` → up to 3 calls.

    Returns
    -------
    SanityReport | None
        ``None`` on missing API key, network failure, or all retries
        rejected by validator. ``SanityReport`` (layer="rules+llm" filled
        upstream) on success.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    if llm_client is None:
        llm_client = _default_llm_call

    system_prompt = _build_system_prompt()
    last_error: str | None = None

    for _attempt in range(max_retries + 1):
        user_prompt = _build_user_prompt(
            writable_items=writable_items,
            pool_size=pool_size,
            recent_runs=recent_runs,
            diagnosis=diagnosis,
            strategy_name=strategy_name,
            asset_hint=asset_hint,
            prev_error=last_error,
        )
        try:
            raw = llm_client(api_key, system_prompt, user_prompt)
        except Exception as exc:
            # Network / http / json — bail to rules. Don't retry network.
            return None

        try:
            obj = json.loads(raw) if isinstance(raw, str) else None
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = f"raw not valid JSON: {exc}"
            continue

        ok, why = _validate_llm_obj(obj or {})
        if not ok:
            last_error = why
            continue

        # Build SanityReport. ``layer`` will be re-tagged by check() once
        # rules + llm are merged, but we set "llm" here for clarity.
        return SanityReport(
            verdict=str(obj["verdict"]),
            score=float(obj["score"]),
            issues=list(obj.get("issues") or []),
            advice=(obj.get("advice") or None),
            summary=str(obj.get("summary") or "").strip(),
            layer="llm",
        )

    return None


# --------------------------------------------------------------------------- #
# Top-level entry
# --------------------------------------------------------------------------- #


def _summary_from_issues(issues: list[dict], strategy_name: str) -> str:
    """One-sentence Chinese summary derived from the hard-rule issues."""
    if not issues:
        return f"硬规则体检通过 ({strategy_name}): 无明显问题"
    fatals = [i for i in issues if i.get("severity") == "fatal"]
    warns = [i for i in issues if i.get("severity") == "warn"]
    parts: list[str] = []
    if fatals:
        codes = ", ".join(i.get("code", "?") for i in fatals[:3])
        parts.append(f"{len(fatals)} 项 fatal ({codes})")
    if warns:
        codes = ", ".join(i.get("code", "?") for i in warns[:3])
        parts.append(f"{len(warns)} 项 warn ({codes})")
    return f"硬规则体检 ({strategy_name}): " + "; ".join(parts)


def _combine_verdict(rule_issues: list[dict], llm_report: SanityReport | None) -> str:
    """Combine rule-level + LLM-level verdicts. fatal beats warn beats ok."""
    rule_has_fatal = any(i.get("severity") == "fatal" for i in rule_issues)
    rule_has_warn = any(i.get("severity") == "warn" for i in rule_issues)

    if rule_has_fatal:
        return "fatal"
    if llm_report is not None and llm_report.verdict == "fatal":
        return "fatal"
    if rule_has_warn:
        return "warn"
    if llm_report is not None and llm_report.verdict == "warn":
        return "warn"
    return "ok"


def check(
    writable_items: list[dict],
    pool_size: int | None,
    recent_runs: list[Any] | None = None,
    diagnosis: dict | None = None,
    strategy_name: str = "unknown",
    asset_hint: str | None = None,
    use_llm: bool = False,
    llm_client: Callable[[str, str, str], str] | None = None,
    max_retries: int = 2,
) -> SanityReport:
    """Top-level entry. Combines hard-rule + (optional) LLM layers.

    Logic:

    1. Run hard rules.
    2. If any rule is ``fatal`` → return immediately as ``fatal`` (do NOT
       call the LLM, even if ``use_llm=True`` — saves money and the
       trajectory is already known to be broken).
    3. Otherwise, if ``use_llm`` is True and the LLM is reachable, call it
       and merge: LLM ``fatal`` overrides rule warns; LLM ``warn`` is OR'd.
    4. ``layer`` is ``"rules"`` if no LLM call happened, ``"rules+llm"``
       if the LLM produced a usable answer.

    Always returns a :class:`SanityReport`; never raises.
    """
    rule_issues = check_hard_rules(writable_items, pool_size)
    rule_has_fatal = any(i.get("severity") == "fatal" for i in rule_issues)

    # Step 2: rule fatal → no LLM call, return immediately.
    if rule_has_fatal:
        verdict = "fatal"
        return SanityReport(
            verdict=verdict,
            score=_RULES_SCORE_MAP[verdict],
            issues=list(rule_issues),
            advice=None,
            summary=_summary_from_issues(rule_issues, strategy_name),
            layer="rules",
        )

    # Step 3: optional LLM layer.
    llm_report: SanityReport | None = None
    if use_llm:
        try:
            llm_report = check_with_llm(
                writable_items=writable_items,
                pool_size=pool_size,
                recent_runs=recent_runs,
                diagnosis=diagnosis,
                strategy_name=strategy_name,
                asset_hint=asset_hint,
                llm_client=llm_client,
                max_retries=max_retries,
            )
        except Exception:
            # Belt-and-braces: never raise.
            llm_report = None

    # Step 4: merge.
    verdict = _combine_verdict(rule_issues, llm_report)

    if llm_report is not None:
        # Merge issues: rules first (they're always factual), then LLM extras.
        merged_issues: list[dict] = list(rule_issues)
        for iss in llm_report.issues:
            merged_issues.append(iss)
        return SanityReport(
            verdict=verdict,
            score=float(llm_report.score),
            issues=merged_issues,
            advice=llm_report.advice,
            summary=llm_report.summary or _summary_from_issues(rule_issues, strategy_name),
            layer="rules+llm",
        )

    # Rules-only path (use_llm=False, or LLM unavailable / rejected).
    return SanityReport(
        verdict=verdict,
        score=_RULES_SCORE_MAP[verdict],
        issues=list(rule_issues),
        advice=None,
        summary=_summary_from_issues(rule_issues, strategy_name),
        layer="rules",
    )


__all__ = [
    "SanityReport",
    "check",
    "check_hard_rules",
    "check_with_llm",
    "DEEPSEEK_ENDPOINT",
    "DEEPSEEK_MODEL",
    "LLM_PERIODIC_GUARD_ITERS",
]
