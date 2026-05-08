#!/usr/bin/env python3
"""
Outbox -> Telegram relay.

Tails ``data/<strategy>/outbox.jsonl`` and forwards a *small* subset of new
lines to a Telegram chat as a short, plain-language message.

Default behaviour is **silent**: routine iteration rows (healthy / stagnant /
recovering / parameter tweaks / sanity warnings) are skipped. Only a handful of
real *events* trigger a push:

    1. New data segment attached       (phase=pool_attached)
    2. Data segment finished           (phase=pool_sealed)
    3. System wants to stop, asks user (phase=stop_approval_requested
                                        OR state first enters
                                        ``pending_stop_approval``)
    4. Hard error                      (phase contains 'error' or is
                                        ``sanity_fatal``; or row has 'error'
                                        field set)
    5. Significant score shift         (|oos_sharpe - last_sent_oos| >= 0.5)
    6. Real stop                       (state enters paused/stopped)

Reads:
  - ``TELEGRAM_BOT_TOKEN`` (or whichever env var passed via ``--token-env``)
    holds the bot token.
  - ``--chat-id`` is the receiving chat id.

Behaviour:
  - On startup, opens ``outbox.jsonl`` and (by default) seeks to EOF so we do
    not re-flood old history. Pass ``--from-start`` to begin at offset 0
    (useful for smoke testing).
  - Polls the file size every ``--poll-secs`` seconds (default 2). When the
    file grows, reads the new bytes, splits on newline, and pushes each
    *worth-pushing* JSON line to Telegram.
  - File rotation / truncation handling: if size shrinks, we re-open and seek
    back to current offset clamped to new size.
  - On Telegram API failure (non-2xx or transport error) retries 3 times with
    exponential backoff (1s, 2s, 4s). Still failing → message is appended to
    a retry-queue file (default ``<outbox>.tg_retry``). On startup we drain
    the retry queue first.
  - SIGTERM / SIGINT: flush any pending retries to disk and exit cleanly.
  - Never raises out of the main loop: any unexpected exception is logged and
    we keep going.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx

LOG = logging.getLogger("outbox_to_telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds; doubles each retry
HTTP_TIMEOUT = 10.0


# --------------------------------------------------------------------------- #
# Formatting — plain-language event messages
# --------------------------------------------------------------------------- #
#
# Design rule: messages MUST NOT contain trader jargon like 夏普 / OOS / IS /
# verdict / phase / stagnant / data_mining / recovering / healthy / audit /
# raw english trace. The format_message dispatcher uses the row's
# phase/state to pick a friendly template, then strips/avoids those words.

# Parameter-name → human-readable Chinese (best-effort; falls back to the
# bare last segment of the dotted path if the strategy uses something we
# haven't catalogued yet).
_PARAM_NAME_ZH = {
    # sp500_grid / csi500_grid / yzm_grid — same shape
    "parameters.grid_count": "网格数",
    "parameters.range_window": "区间窗口(天)",
    "parameters.position_per_grid": "单格仓位",
    "rules.fee_pct": "手续费率",
    # cb_redemption
    "parameters.w_redeem_progress": "强赎进度权重",
    "parameters.w_premium_ratio": "溢价率权重",
    "parameters.w_remaining_size": "剩余规模权重",
    "parameters.w_stock_momentum": "正股动量权重",
    "parameters.w_market_sentiment": "市场情绪权重",
    "thresholds.action": "买入阈值",
    "thresholds.alert": "预警阈值",
    "thresholds.watch": "观察阈值",
    "rules.hold_max_days": "最长持有天数",
    "rules.target_exit_pct": "止盈百分比",
    "rules.stop_loss_pct": "止损百分比",
    "rules.max_positions": "最大仓位数",
    "rules.top_k": "候选数",
}


def _zh_param(item_path: str | None) -> str:
    if not item_path:
        return "参数"
    return _PARAM_NAME_ZH.get(item_path, item_path.split(".")[-1])


def _label_prefix(label: str | None) -> str:
    return f"[{label}] " if label else ""


def _first_sentence(text: str | None, max_chars: int = 50) -> str:
    """Take the first sentence (cut at first ./。/!/?/!/?) up to max_chars.

    Returns "" if no usable text. Falls back to the head if no terminator
    found within the limit.
    """
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    # Find first sentence terminator within the first ~max_chars*2 chars.
    cut_re = re.compile(r"[.。!?!?;；\n]")
    head = s[: max_chars * 3]
    m = cut_re.search(head)
    if m:
        s = head[: m.start()].strip()
    if len(s) > max_chars:
        s = s[: max_chars].rstrip() + "…"
    return s


def _humanise_paused_reason(reason: str | None) -> str:
    """Translate orchestrator-internal reason strings to plain Chinese.

    Covers the well-known ones; falls back to the raw string (already short).
    """
    if not reason:
        return "(原因未知)"
    r = str(reason)
    table = [
        ("all holdout pools exhausted", "隔离样本袋全用完了"),
        ("holdout dry", "隔离样本袋全用完了"),
        ("max_recovery_attempts", "试了几种修法都没救回来"),
        ("max recovery attempts", "试了几种修法都没救回来"),
        ("user approved stop", "用户确认了停止"),
        ("user-approved stop", "用户确认了停止"),
        ("control.signal=pause", "用户手动按了暂停"),
        ("control.signal=stop", "用户手动按了停止"),
        ("user requested stop", "用户请求停止"),
        ("hypothesizer returned None", "参数搜光了, 没想出新方向"),
        ("no untouched", "参数搜光了, 没想出新方向"),
        ("hypothesizer", "参数搜光了, 没想出新方向"),
        ("editor blocked", "改参数被卡住"),
        ("editor", "改参数被卡住"),
        ("auditor veto", "审计否决了"),
        ("auditor", "审计否决了"),
        ("3 stagnant", "连续几轮没进展"),
        ("stagnant", "连续几轮没进展"),
    ]
    low = r.lower()
    for needle, zh in table:
        if needle.lower() in low:
            return zh
    # Fallback: keep it short.
    return r if len(r) < 60 else r[:57] + "…"


def _fmt_pool_attached(row: dict[str, Any], label: str | None) -> str:
    pool_id = row.get("pool_id")
    n_events = row.get("n_events")
    head = f"📍 {_label_prefix(label)}换到新一段历史"
    parts = [head]
    if pool_id is not None:
        parts.append(f"   段编号: 第 {pool_id} 段")
    if isinstance(n_events, int) and n_events > 0:
        parts.append(f"   共 {n_events} 个交易日")
    return "\n".join(parts)


def _fmt_pool_sealed(row: dict[str, Any], label: str | None) -> str:
    pool_id = row.get("pool_id")
    iters = row.get("iters_spent")
    seg_no_txt = f"第 {pool_id + 1} 段" if isinstance(pool_id, int) else "当前段"
    iters_txt = f"(共 {iters} 轮调参)" if isinstance(iters, int) and iters > 0 else ""
    head = f"📊 {_label_prefix(label)}{seg_no_txt}跑完{iters_txt}".rstrip()
    return head + "\n   跑完了, 自动接下一段"


def _fmt_stop_approval(row: dict[str, Any], label: str | None) -> str:
    reason = _humanise_paused_reason(row.get("paused_reason"))
    head = f"⚠️ {_label_prefix(label)}系统想停下"
    parts = [
        head,
        f"   原因: {reason}",
        "   1 分钟内回我:",
        "     stop = 真停",
        "     continue = 别停, 接着跑当前方向",
        "     shift = 重置参数, 从新位置探索",
        "   不回复 → 自动 shift 重置继续",
    ]
    return "\n".join(parts)


def _fmt_error(row: dict[str, Any], label: str | None) -> str:
    err_raw = row.get("error") or row.get("paused_reason") or row.get("phase") or "未知错误"
    err = str(err_raw)
    if len(err) > 120:
        err = err[:117] + "…"
    head = f"❌ {_label_prefix(label)}出问题了"
    return f"{head}\n   {err}"


def _fmt_paused(row: dict[str, Any], label: str | None) -> str:
    reason = _humanise_paused_reason(row.get("paused_reason"))
    head = f"🛑 {_label_prefix(label)}系统真停了"
    return f"{head}\n   原因: {reason}"


def _fmt_score_jump(
    row: dict[str, Any],
    prev_row: dict[str, Any] | None,
    label: str | None,
) -> str:
    """Significant score change. We pull the LAST sent score from FilterState
    via ``prev_row`` (a tiny synthesised dict)."""
    cur = row.get("oos_sharpe")
    prev = (prev_row or {}).get("oos_sharpe")
    cur_f = float(cur) if isinstance(cur, (int, float)) else None
    prev_f = float(prev) if isinstance(prev, (int, float)) else None
    direction_up = (
        cur_f is not None and prev_f is not None and cur_f > prev_f
    )

    if direction_up:
        head = f"📈 {_label_prefix(label)}成绩跳了一档(变好) — 注意!"
    else:
        head = f"📉 {_label_prefix(label)}成绩突然变差"

    parts = [head]
    if prev_f is not None and cur_f is not None:
        parts.append(f"   上次: {prev_f:+.2f} → 现在: {cur_f:+.2f}")
    elif cur_f is not None:
        parts.append(f"   现在: {cur_f:+.2f}")

    # Recent change description, if structured fields are present.
    item_path = row.get("change_item_path")
    new_value = row.get("change_new_value")
    old_value = row.get("change_old_value")
    if item_path and new_value is not None:
        zh = _zh_param(item_path)
        if old_value is not None and old_value != new_value:
            parts.append(f"   最近改的: {zh} 从 {old_value} → {new_value}")
        else:
            parts.append(f"   最近改的: {zh} → {new_value}")

    # First sentence of the LLM/rule reason.
    reason = _first_sentence(row.get("hypothesis_reason"), max_chars=50)
    if reason:
        parts.append(f"   说明: {reason}")

    return "\n".join(parts)


# Phases the orchestrator emits when something errored out.
_ERROR_PHASES = {
    "sanity_fatal",
    "sanity_error",
    "git_commit_error",
    "verifier_error",
    "judge_error",
    "auditor_error",
    "memory_error",
    "hypothesizer_error",
    "editor_error",
    "holdout_seal_error",
    "holdout_remaining_error",
    "holdout_read_error",
}


def _is_error_row(row: dict[str, Any]) -> bool:
    if row.get("error"):
        return True
    phase = str(row.get("phase") or "").lower()
    if phase in _ERROR_PHASES:
        return True
    if "error" in phase:
        return True
    return False


def format_message(
    row: dict[str, Any],
    label: str | None = None,
    prev_row: dict[str, Any] | None = None,
) -> str:
    """Pick the right friendly template based on row contents.

    ``prev_row`` provides the previous *sent* row (for score-jump messages
    that need the prior oos_sharpe). FilterState owns and supplies it.
    """
    phase = str(row.get("phase") or "")
    state = str(row.get("state") or "")

    if phase == "pool_attached":
        return _fmt_pool_attached(row, label)
    if phase == "pool_sealed":
        return _fmt_pool_sealed(row, label)
    if phase == "stop_approval_requested" or state == "pending_stop_approval":
        return _fmt_stop_approval(row, label)
    if _is_error_row(row):
        return _fmt_error(row, label)
    if state in {"paused", "stopped"}:
        return _fmt_paused(row, label)
    # FilterState only routes here for a real score jump.
    return _fmt_score_jump(row, prev_row, label)


# --------------------------------------------------------------------------- #
# Telegram client (retries + queue)
# --------------------------------------------------------------------------- #


class TelegramRelay:
    """Thin wrapper that POSTs to Telegram with retry + on-disk retry queue."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        retry_queue_path: Path,
        client: httpx.Client | None = None,
        sleep_fn: Any = time.sleep,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE,
    ) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.retry_queue_path = retry_queue_path
        self.client = client or httpx.Client(timeout=HTTP_TIMEOUT)
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    # -- low-level send -----------------------------------------------------

    def _post_once(self, text: str) -> bool:
        url = TELEGRAM_API.format(token=self.token)
        payload = {"chat_id": self.chat_id, "text": text}
        try:
            resp = self.client.post(url, json=payload)
        except Exception as exc:  # transport error
            LOG.warning("telegram POST transport error: %s", exc)
            return False
        if 200 <= resp.status_code < 300:
            return True
        # log body for debug, then fail
        body = ""
        try:
            body = resp.text[:200]
        except Exception:
            pass
        LOG.warning(
            "telegram POST returned %s: %s", resp.status_code, body
        )
        return False

    def send(self, text: str) -> bool:
        """Send text. Returns True on success. On total failure, queues."""
        for attempt in range(self.max_retries):
            if self._post_once(text):
                return True
            if attempt < self.max_retries - 1:
                self.sleep_fn(self.backoff_base * (2 ** attempt))
        # All retries exhausted → queue.
        self._enqueue(text)
        return False

    # -- retry queue --------------------------------------------------------

    def _enqueue(self, text: str) -> None:
        try:
            self.retry_queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self.retry_queue_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
            LOG.info("queued message to %s", self.retry_queue_path)
        except Exception as exc:
            LOG.error("failed to enqueue retry message: %s", exc)

    def drain_queue(self) -> int:
        """Try to send everything in the retry queue. Returns # successes."""
        if not self.retry_queue_path.exists():
            return 0
        try:
            lines = self.retry_queue_path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            LOG.error("failed reading retry queue: %s", exc)
            return 0
        successes = 0
        leftover: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                text = payload.get("text", "")
            except Exception:
                continue
            if not text:
                continue
            ok = False
            for attempt in range(self.max_retries):
                if self._post_once(text):
                    ok = True
                    break
                if attempt < self.max_retries - 1:
                    self.sleep_fn(self.backoff_base * (2 ** attempt))
            if ok:
                successes += 1
            else:
                leftover.append(line)
        # rewrite queue with only the still-failing entries
        try:
            if leftover:
                self.retry_queue_path.write_text(
                    "\n".join(leftover) + "\n", encoding="utf-8"
                )
            else:
                self.retry_queue_path.unlink(missing_ok=True)
        except Exception as exc:
            LOG.error("failed rewriting retry queue: %s", exc)
        return successes

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Tailer
# --------------------------------------------------------------------------- #


class OutboxTailer:
    """Polls an append-only file and yields complete new lines."""

    def __init__(
        self,
        path: Path,
        from_start: bool = False,
        poll_secs: float = 2.0,
        sleep_fn: Any = time.sleep,
    ) -> None:
        self.path = path
        self.from_start = from_start
        self.poll_secs = poll_secs
        self.sleep_fn = sleep_fn
        self._offset = 0
        self._buf = ""
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _initial_offset(self) -> int:
        if self.from_start:
            return 0
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _read_new(self) -> list[str]:
        """Read any new bytes from current offset; return complete lines."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []

        if size < self._offset:
            # truncated/rotated → restart at 0
            LOG.info("outbox shrunk (rotation?) — resetting offset")
            self._offset = 0
            self._buf = ""

        if size == self._offset:
            return []

        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except FileNotFoundError:
            return []

        self._offset = size
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            return []

        self._buf += text
        out: list[str] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                out.append(line)
        return out

    def run(self, on_line: Any) -> None:
        self._offset = self._initial_offset()
        LOG.info(
            "tailing %s from offset=%d (from_start=%s)",
            self.path, self._offset, self.from_start,
        )
        while not self._stop:
            try:
                lines = self._read_new()
            except Exception as exc:
                LOG.error("tailer error: %s", exc)
                lines = []
            for line in lines:
                try:
                    on_line(line)
                except Exception as exc:
                    LOG.error("on_line callback raised: %s", exc)
            if self._stop:
                break
            self.sleep_fn(self.poll_secs)


# --------------------------------------------------------------------------- #
# Filter — only forward 6 event categories
# --------------------------------------------------------------------------- #


# Phases that are *always* worth pushing (data segment milestones, real
# errors, structured stop-approval requests).
_ALWAYS_PHASES = {
    "pool_attached",
    "pool_sealed",
    "stop_approval_requested",
    "sanity_fatal",
    "git_commit_error",
    "verifier_error",
    "judge_error",
    "auditor_error",
    "memory_error",
    "hypothesizer_error",
    "editor_error",
    "holdout_seal_error",
    "holdout_remaining_error",
    "holdout_read_error",
}


class FilterState:
    """Decide whether each outbox row is worth a Telegram push.

    Default policy: silent. Forward only when one of the 6 events fires:

      1. phase ∈ _ALWAYS_PHASES  (segment milestones, structured errors)
      2. row['error'] truthy     (any error path)
      3. state first transitions into 'pending_stop_approval'
      4. state first transitions into 'paused' or 'stopped'
      5. |oos_sharpe - last_sent_oos| >= oos_delta (significant score shift)
      6. heartbeat_every > 0 AND iter % heartbeat_every == 0  (off by default)

    Routine healthy / stagnant / recovering / sanity_warn / sanity-pool-stats
    rows are dropped.
    """

    def __init__(
        self,
        heartbeat_every: int = 0,
        oos_delta: float = 0.5,
    ) -> None:
        self.heartbeat_every = max(0, heartbeat_every)
        self.oos_delta = oos_delta
        self.last_sent_state: str | None = None
        self.last_sent_oos: float | None = None
        self.last_sent_iter: int | None = None
        # Snapshot of the last *sent* row, used by score-jump formatter to
        # show "上次 → 现在" deltas.
        self.last_sent_row: dict[str, Any] | None = None

    def should_send(self, row: dict[str, Any]) -> bool:
        phase = str(row.get("phase") or "")
        state = str(row.get("state") or "")

        # 1. Always-push phases.
        if phase in _ALWAYS_PHASES:
            return True

        # 2. Any error-bearing row.
        if row.get("error"):
            return True
        if "error" in phase.lower():
            return True

        # 3. First entry into pending_stop_approval.
        if state == "pending_stop_approval" and self.last_sent_state != "pending_stop_approval":
            return True

        # 4. First entry into paused/stopped (skips repeated paused heartbeats).
        if state in {"paused", "stopped"} and self.last_sent_state not in {"paused", "stopped"}:
            return True

        # 5. Significant score shift.
        oos = row.get("oos_sharpe")
        if isinstance(oos, (int, float)) and isinstance(self.last_sent_oos, (int, float)):
            if abs(float(oos) - float(self.last_sent_oos)) >= self.oos_delta:
                return True

        # 6. Optional heartbeat (default OFF).
        it = row.get("iteration")
        if (
            self.heartbeat_every > 0
            and isinstance(it, int)
            and it > 0
            and it % self.heartbeat_every == 0
            and it != self.last_sent_iter
        ):
            return True

        return False

    def mark_sent(self, row: dict[str, Any]) -> None:
        if "state" in row and row["state"] is not None:
            self.last_sent_state = str(row["state"])
        if isinstance(row.get("oos_sharpe"), (int, float)):
            self.last_sent_oos = float(row["oos_sharpe"])
        if isinstance(row.get("iteration"), int):
            self.last_sent_iter = row["iteration"]
        # Snapshot only the fields the score-jump formatter cares about.
        snap_keys = (
            "iteration", "oos_sharpe", "oos_trades", "oos_return",
            "change_item_path", "change_new_value", "change_old_value",
            "hypothesis_reason",
        )
        self.last_sent_row = {k: row.get(k) for k in snap_keys}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def _handle_line(
    line: str,
    relay: TelegramRelay,
    filt: FilterState | None = None,
    label: str | None = None,
) -> None:
    try:
        row = json.loads(line)
    except Exception as exc:
        LOG.warning("skipping malformed json line: %s", exc)
        return
    if filt is not None and not filt.should_send(row):
        LOG.debug(
            "filter: skipping iter=%s phase=%s state=%s",
            row.get("iteration"), row.get("phase"), row.get("state"),
        )
        return
    prev_row = filt.last_sent_row if filt is not None else None
    text = format_message(row, label=label, prev_row=prev_row)
    if relay.send(text) and filt is not None:
        filt.mark_sent(row)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tail outbox.jsonl → Telegram")
    p.add_argument("--outbox", required=True, help="Path to outbox.jsonl")
    p.add_argument("--chat-id", required=True, help="Telegram chat id")
    p.add_argument(
        "--token-env",
        default="TELEGRAM_BOT_TOKEN",
        help="Env var name holding the bot token",
    )
    p.add_argument(
        "--from-start",
        action="store_true",
        help="Tail from offset 0 instead of EOF (testing only)",
    )
    p.add_argument("--poll-secs", type=float, default=2.0)
    p.add_argument(
        "--retry-queue",
        default=None,
        help="Path to retry queue file (default <outbox>.tg_retry)",
    )
    p.add_argument(
        "--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR"
    )
    p.add_argument(
        "--no-filter", action="store_true",
        help="Disable noise filter (push every outbox row; default: filter)",
    )
    p.add_argument(
        "--heartbeat-every", type=int, default=0,
        help="Push every Nth iteration even if nothing changed (default 0 = OFF)",
    )
    p.add_argument(
        "--oos-delta", type=float, default=0.5,
        help="Push when score shifts by >= this from last sent (default 0.5)",
    )
    p.add_argument(
        "--label", default=None,
        help="Prefix every message head with [LABEL] (e.g. 'sp500-grid')",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        sys.stderr.write(
            f"ERROR: env var {args.token_env} is empty or missing.\n"
            "       Set it (e.g. via systemd EnvironmentFile) and retry.\n"
        )
        return 1

    outbox_path = Path(args.outbox)
    retry_queue = (
        Path(args.retry_queue)
        if args.retry_queue
        else outbox_path.with_suffix(outbox_path.suffix + ".tg_retry")
    )

    relay = TelegramRelay(
        token=token,
        chat_id=args.chat_id,
        retry_queue_path=retry_queue,
    )
    # Drain anything left from last run before tailing.
    try:
        n = relay.drain_queue()
        if n:
            LOG.info("drained %d queued messages on startup", n)
    except Exception as exc:
        LOG.error("drain_queue failed: %s", exc)

    tailer = OutboxTailer(
        path=outbox_path,
        from_start=args.from_start,
        poll_secs=args.poll_secs,
    )

    filt: FilterState | None = None
    if not args.no_filter:
        filt = FilterState(
            heartbeat_every=args.heartbeat_every,
            oos_delta=args.oos_delta,
        )
        LOG.info(
            "filter on: heartbeat=%d oos_delta=%.3f (use --no-filter to push everything)",
            args.heartbeat_every, args.oos_delta,
        )
    else:
        LOG.info("filter off: pushing every row")

    def _shutdown(signum: int, _frame: Any) -> None:
        LOG.info("received signal %d, shutting down", signum)
        tailer.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        tailer.run(lambda line: _handle_line(line, relay, filt, label=args.label))
    except Exception as exc:
        LOG.error("tailer crashed: %s", exc)
    finally:
        relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
