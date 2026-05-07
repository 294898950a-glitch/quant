#!/usr/bin/env python3
"""
Outbox -> Telegram relay.

Tails ``data/cb_redemption/outbox.jsonl`` and forwards each new line to a
Telegram chat as a short markdown message.

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
    complete JSON line to Telegram.
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
# Formatting
# --------------------------------------------------------------------------- #


def format_message(row: dict[str, Any], label: str | None = None) -> str:
    """Turn one outbox row into a short markdown message for Telegram."""
    iteration = row.get("iteration", "?")
    verdict = row.get("verdict") or row.get("phase") or "n/a"
    state = row.get("state") or ""
    paused_reason = row.get("paused_reason")
    change_summary = row.get("change_summary") or ""
    error = row.get("error")

    oos_raw = row.get("oos_sharpe")
    if isinstance(oos_raw, (int, float)):
        oos_str = f"{float(oos_raw):.2f}"
    else:
        oos_str = "n/a"

    bang = ""
    state_lower = str(state).lower()
    if state_lower in {"paused", "error"} or "error" in str(verdict).lower():
        bang = "❗ "  # red exclamation

    label_prefix = f"[{label}] " if label else ""
    head = f"{bang}{label_prefix}iter={iteration} | {verdict} | OOS={oos_str}"
    parts = [head]
    if change_summary:
        parts.append(f"change: {change_summary}")
    if state:
        line = f"state: {state}"
        if paused_reason:
            line += f" ({paused_reason})"
        parts.append(line)
    if error:
        parts.append(f"error: {error}")
    return "\n".join(parts)


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
# Filter — only forward "interesting" lines
# --------------------------------------------------------------------------- #


# Phases we always push (state transitions, milestones, errors)
_ALWAYS_PHASES = {
    "paused", "stopped", "vetoed",
    "pool_attached", "pool_sealed",
    "git_commit_error",
}


class FilterState:
    """Decide whether each outbox row is worth a telegram push.

    Stateful across rows in one process. Forwards only:
      - phase in _ALWAYS_PHASES
      - any 'error' field present
      - state transition (state field differs from last sent)
      - verdict transition (verdict field differs from last sent)
      - oos_sharpe changed by >= delta from last sent
      - every Nth iteration as a quiet heartbeat
    """

    def __init__(self, heartbeat_every: int = 20, oos_delta: float = 0.05) -> None:
        self.heartbeat_every = max(1, heartbeat_every)
        self.oos_delta = oos_delta
        self.last_sent_state: str | None = None
        self.last_sent_verdict: str | None = None
        self.last_sent_oos: float | None = None
        self.last_sent_iter: int | None = None

    def should_send(self, row: dict[str, Any]) -> bool:
        phase = str(row.get("phase") or "").lower()
        if phase in _ALWAYS_PHASES:
            return True
        if row.get("error"):
            return True

        state = row.get("state")
        verdict = row.get("verdict")
        oos = row.get("oos_sharpe")
        it = row.get("iteration")

        # Transitions are interesting.
        if state is not None and state != self.last_sent_state:
            return True
        if verdict is not None and verdict != self.last_sent_verdict:
            return True

        # Significant OOS movement.
        if isinstance(oos, (int, float)) and isinstance(self.last_sent_oos, (int, float)):
            if abs(float(oos) - float(self.last_sent_oos)) >= self.oos_delta:
                return True

        # Heartbeat every Nth iteration even if nothing else changed.
        if isinstance(it, int) and it > 0 and it % self.heartbeat_every == 0:
            return True

        return False

    def mark_sent(self, row: dict[str, Any]) -> None:
        if "state" in row and row["state"] is not None:
            self.last_sent_state = row["state"]
        if "verdict" in row and row["verdict"] is not None:
            self.last_sent_verdict = row["verdict"]
        if isinstance(row.get("oos_sharpe"), (int, float)):
            self.last_sent_oos = float(row["oos_sharpe"])
        if isinstance(row.get("iteration"), int):
            self.last_sent_iter = row["iteration"]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def _handle_line(line: str, relay: TelegramRelay, filt: FilterState | None = None,
                 label: str | None = None) -> None:
    try:
        row = json.loads(line)
    except Exception as exc:
        LOG.warning("skipping malformed json line: %s", exc)
        return
    if filt is not None and not filt.should_send(row):
        LOG.debug("filter: skipping iter=%s phase=%s", row.get("iteration"), row.get("phase"))
        return
    text = format_message(row, label=label)
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
        "--heartbeat-every", type=int, default=20,
        help="Push every Nth iteration even if nothing changed (default 20)",
    )
    p.add_argument(
        "--oos-delta", type=float, default=0.05,
        help="Push when oos_sharpe shifts by >= this from last sent (default 0.05)",
    )
    p.add_argument(
        "--label", default=None,
        help="Prefix every message head with [LABEL] (e.g. 'sp500-grid'); default: no prefix",
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
