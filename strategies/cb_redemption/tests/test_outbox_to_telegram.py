"""Tests for ``scripts/outbox_to_telegram.py`` relay.

All tests stub httpx and use ``tmp_path`` for outbox + retry queue. None hit
the real Telegram API.

The relay is **silent by default**: only 6 event categories trigger a push
(see ``FilterState`` docstring). Routine iterations are dropped. Messages
must be plain Chinese — no trader jargon.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# Module loader (scripts/ has no __init__; load by path)
# --------------------------------------------------------------------------- #


_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "outbox_to_telegram.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "outbox_to_telegram_under_test", _SCRIPT
    )
    assert spec and spec.loader, "could not load outbox_to_telegram.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


otg = _load_module()


# --------------------------------------------------------------------------- #
# Banned-jargon helper — every rendered telegram message must avoid these.
# --------------------------------------------------------------------------- #


_BANNED = (
    "夏普", "OOS", "IS",  # raw english/chinese trader jargon
    "verdict", "phase",
    "stagnant", "data_mining", "recovering", "healthy",
    "audit", "veto", "vetoed",
)


def _assert_no_jargon(msg: str) -> None:
    for word in _BANNED:
        assert word not in msg, (
            f"banned word {word!r} appeared in message:\n{msg}"
        )


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHttpxClient:
    """Records POST calls and returns canned responses."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        # responses: list of int (status code) OR Exception (will be raised)
        self.responses = list(responses or [200])
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        # consume one response; default to 200 if exhausted
        if self.responses:
            r = self.responses.pop(0)
        else:
            r = 200
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            code, text = r
            return _FakeResponse(code, text)
        return _FakeResponse(int(r))

    def close(self) -> None:
        self.closed = True


def _fake_sleep_recorder() -> tuple[list[float], Any]:
    log: list[float] = []

    def _sleep(s: float) -> None:
        log.append(s)

    return log, _sleep


def _make_relay(
    tmp_path: Path,
    responses: list[Any] | None = None,
) -> tuple[Any, _FakeHttpxClient, list[float]]:
    client = _FakeHttpxClient(responses=responses)
    log, sleep = _fake_sleep_recorder()
    relay = otg.TelegramRelay(
        token="TT",
        chat_id="6403706808",
        retry_queue_path=tmp_path / "retry.jsonl",
        client=client,
        sleep_fn=sleep,
        max_retries=3,
        backoff_base=0.01,
    )
    return relay, client, log


# --------------------------------------------------------------------------- #
# 1. format_message — event templates
# --------------------------------------------------------------------------- #


def test_pool_attached_message_friendly() -> None:
    """phase=pool_attached → 'changed to a new history segment', no jargon."""
    row = {"iteration": 0, "phase": "pool_attached",
           "pool_id": 3, "n_events": 130}
    msg = otg.format_message(row, label="sp500-grid")
    assert "[sp500-grid]" in msg
    assert "换到新一段" in msg
    assert "130" in msg
    _assert_no_jargon(msg)


def test_pool_sealed_message() -> None:
    row = {"iteration": 30, "phase": "pool_sealed",
           "pool_id": 3, "iters_spent": 30}
    msg = otg.format_message(row, label="sp500-grid")
    # pool 3 (0-indexed) → "第 4 段"
    assert "第 4 段" in msg
    assert "跑完" in msg
    assert "30 轮" in msg
    _assert_no_jargon(msg)


def test_stop_approval_message_lists_user_options() -> None:
    row = {
        "iteration": 12, "phase": "stop_approval_requested",
        "verdict": "stagnant", "state": "pending_stop_approval",
        "paused_reason": "hypothesizer returned None 3 in a row",
    }
    msg = otg.format_message(row, label="sp500-grid")
    assert "想停下" in msg
    assert "stop" in msg
    assert "continue" in msg
    assert "shift" in msg
    # Friendly translation of the internal reason.
    assert "参数搜光了" in msg
    _assert_no_jargon(msg)


def test_paused_message_humanises_reason() -> None:
    row = {"iteration": 50, "phase": "paused", "state": "paused",
           "paused_reason": "all holdout pools exhausted"}
    msg = otg.format_message(row, label="cb")
    assert "真停了" in msg
    assert "隔离样本袋全用完了" in msg
    _assert_no_jargon(msg)


def test_error_message() -> None:
    row = {"iteration": 7, "phase": "git_commit_error",
           "error": "git commit returned 128"}
    msg = otg.format_message(row, label="sp500-grid")
    assert "出问题了" in msg
    assert "128" in msg
    _assert_no_jargon(msg)


def test_score_jump_message_includes_delta_and_first_sentence() -> None:
    """Score jump: shows previous → current, plus the change & one-sentence reason."""
    prev_row = {"iteration": 5, "oos_sharpe": -1.2}
    row = {
        "iteration": 6, "phase": "healthy", "state": "running",
        "oos_sharpe": 0.6, "oos_trades": 91, "oos_return": 0.2,
        "change_item_path": "parameters.grid_count",
        "change_old_value": 18, "change_new_value": 24,
        "change_source": "llm",
        "hypothesis_reason": "格距过密导致频繁震荡。后面这段不要出现在消息里。",
    }
    msg = otg.format_message(row, label="sp500-grid", prev_row=prev_row)
    assert "成绩跳了一档(变好)" in msg
    assert "-1.20" in msg
    assert "+0.60" in msg
    assert "网格数" in msg
    assert "18" in msg and "24" in msg
    # Only the first sentence of the reason.
    assert "格距过密导致频繁震荡" in msg
    assert "后面这段不要" not in msg
    _assert_no_jargon(msg)


def test_score_drop_message() -> None:
    prev_row = {"iteration": 5, "oos_sharpe": 0.5}
    row = {
        "iteration": 6, "phase": "healthy", "state": "running",
        "oos_sharpe": -1.8,
    }
    msg = otg.format_message(row, label="sp500-grid", prev_row=prev_row)
    assert "成绩突然变差" in msg
    assert "+0.50" in msg
    assert "-1.80" in msg
    _assert_no_jargon(msg)


def test_message_text_no_jargon_across_all_phases() -> None:
    """No matter what phase, the rendered text MUST avoid the banned vocab."""
    rows = [
        {"phase": "pool_attached", "pool_id": 0, "n_events": 99},
        {"phase": "pool_sealed", "pool_id": 0, "iters_spent": 30},
        {"phase": "stop_approval_requested", "state": "pending_stop_approval",
         "paused_reason": "hypothesizer returned None"},
        {"phase": "paused", "state": "paused",
         "paused_reason": "all holdout pools exhausted"},
        {"phase": "git_commit_error", "error": "commit failed"},
        {"phase": "sanity_fatal", "paused_reason": "sanity panic"},
        {"phase": "healthy", "state": "running", "oos_sharpe": 0.6},
    ]
    prev = {"iteration": 1, "oos_sharpe": -1.0}
    for r in rows:
        msg = otg.format_message(r, label="sp500-grid", prev_row=prev)
        _assert_no_jargon(msg)


def test_message_text_short() -> None:
    """All template renders must stay under 250 chars."""
    rows = [
        {"phase": "pool_attached", "pool_id": 0, "n_events": 99},
        {"phase": "pool_sealed", "pool_id": 7, "iters_spent": 30},
        {"phase": "stop_approval_requested", "state": "pending_stop_approval",
         "paused_reason": "hypothesizer returned None 3 times"},
        {"phase": "paused", "state": "paused",
         "paused_reason": "all holdout pools exhausted"},
        {"phase": "git_commit_error", "error": "x" * 300},  # truncates
        {"phase": "healthy", "state": "running", "oos_sharpe": 0.6,
         "change_item_path": "parameters.grid_count",
         "change_old_value": 18, "change_new_value": 24,
         "hypothesis_reason": "y" * 300},
    ]
    prev = {"oos_sharpe": -1.0}
    for r in rows:
        msg = otg.format_message(r, label="sp500-grid", prev_row=prev)
        assert len(msg) <= 250, f"too long ({len(msg)} chars):\n{msg}"


# --------------------------------------------------------------------------- #
# 2. FilterState — silence by default
# --------------------------------------------------------------------------- #


def test_routine_healthy_iter_not_sent() -> None:
    """Routine running rows with tiny score wobble: dropped."""
    f = otg.FilterState()  # defaults: heartbeat=0, oos_delta=0.5
    f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": 0.0})

    for i in range(2, 50):
        row = {"iteration": i, "phase": "healthy", "state": "running",
               "oos_sharpe": 0.05}
        assert f.should_send(row) is False, (
            f"iter {i} routine row should not push; oos delta tiny"
        )


def test_pool_attached_sent_with_friendly_text() -> None:
    """phase=pool_attached → push; message contains '换到新一段', no 'phase'."""
    f = otg.FilterState()
    row = {"iteration": 0, "phase": "pool_attached",
           "pool_id": 0, "n_events": 25}
    assert f.should_send(row) is True
    msg = otg.format_message(row, label="sp500-grid")
    assert "换到新一段" in msg
    _assert_no_jargon(msg)


def test_pool_sealed_sent() -> None:
    f = otg.FilterState()
    row = {"iteration": 30, "phase": "pool_sealed",
           "pool_id": 0, "iters_spent": 30}
    assert f.should_send(row) is True
    msg = otg.format_message(row, label="sp500-grid")
    assert "第 1 段" in msg and "跑完" in msg
    _assert_no_jargon(msg)


def test_pending_stop_approval_sent_first_time_only() -> None:
    """First entry into pending_stop_approval pushes; subsequent rows in that
    state do not re-push (avoid spam while waiting for user reply)."""
    f = otg.FilterState()
    f.mark_sent({"iteration": 5, "state": "running", "oos_sharpe": 0.0})

    # First time entering pending: send.
    row1 = {"iteration": 6, "phase": "stop_approval_requested",
            "state": "pending_stop_approval",
            "paused_reason": "hypothesizer returned None"}
    assert f.should_send(row1) is True
    f.mark_sent(row1)

    # Subsequent rows still in pending without phase=stop_approval_requested:
    # should NOT push again.
    row2 = {"iteration": 7, "phase": "healthy",
            "state": "pending_stop_approval", "oos_sharpe": 0.0}
    assert f.should_send(row2) is False

    row3 = {"iteration": 8, "phase": "healthy",
            "state": "pending_stop_approval", "oos_sharpe": 0.05}
    assert f.should_send(row3) is False


def test_score_jump_05_sent() -> None:
    """OOS shifts by >= 0.5 (default delta) → push."""
    f = otg.FilterState()
    f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": -1.2})
    big = {"iteration": 2, "phase": "healthy", "state": "running",
           "oos_sharpe": 0.6}  # delta = 1.8
    assert f.should_send(big) is True


def test_score_tiny_jump_not_sent() -> None:
    """OOS shift < 0.5 → not pushed."""
    f = otg.FilterState()
    f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": 0.7})
    small = {"iteration": 2, "phase": "healthy", "state": "running",
             "oos_sharpe": 0.8}  # delta = 0.1
    assert f.should_send(small) is False


def test_error_phases_always_sent() -> None:
    """Every known error phase pushes regardless of last_sent_state."""
    error_phases = [
        "sanity_fatal", "git_commit_error", "verifier_error",
        "judge_error", "auditor_error", "memory_error",
        "hypothesizer_error", "editor_error",
    ]
    for ph in error_phases:
        f = otg.FilterState()
        f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": 0.0})
        row = {"iteration": 2, "phase": ph, "error": f"boom in {ph}"}
        assert f.should_send(row) is True, f"{ph} should always push"


def test_paused_first_time_sent() -> None:
    """state=paused first time → push; staying paused does NOT re-push."""
    f = otg.FilterState()
    f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": 0.0})

    row1 = {"iteration": 2, "phase": "paused", "state": "paused",
            "paused_reason": "all holdout pools exhausted"}
    assert f.should_send(row1) is True
    f.mark_sent(row1)

    row2 = {"iteration": 3, "phase": "healthy", "state": "paused",
            "oos_sharpe": 0.0}
    assert f.should_send(row2) is False


def test_routine_recovering_iter_not_sent() -> None:
    """Recovery iterations alone don't push — internal book-keeping."""
    f = otg.FilterState()
    f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": 0.0})
    row = {"iteration": 2, "phase": "recovering", "state": "running",
           "oos_sharpe": 0.0}
    assert f.should_send(row) is False


def test_sanity_warn_not_sent() -> None:
    """sanity_warn rows are not interesting to user."""
    f = otg.FilterState()
    f.mark_sent({"iteration": 1, "state": "running", "oos_sharpe": 0.0})
    row = {"iteration": 2, "phase": "sanity_warn", "state": "running",
           "oos_sharpe": 0.05}
    assert f.should_send(row) is False


def test_pool_stats_not_sent() -> None:
    """pool_stats rows (printed after pool_attached) are debug — drop."""
    f = otg.FilterState()
    row = {"iteration": 0, "phase": "pool_stats", "pool_id": 0,
           "stats": {"n": 25}}
    # First state transition would also fire — but pool_stats has no state.
    assert f.should_send(row) is False


# --------------------------------------------------------------------------- #
# 3. Happy path: one new line of an EVENT type → one telegram call
# --------------------------------------------------------------------------- #


def test_tail_pool_attached_sends_once(tmp_path: Path) -> None:
    """Only an event-row gets pushed; the tailer writes one row, one POST."""
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("")
    relay, client, _ = _make_relay(tmp_path)

    tailer = otg.OutboxTailer(path=outbox, from_start=False, poll_secs=0.01)

    row = {"iteration": 0, "phase": "pool_attached",
           "pool_id": 0, "n_events": 25}

    def _producer() -> None:
        time.sleep(0.05)
        with outbox.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        time.sleep(0.1)
        tailer.stop()

    filt = otg.FilterState()
    t = threading.Thread(target=_producer)
    t.start()
    tailer.run(lambda line: otg._handle_line(line, relay, filt, label="sp500-grid"))
    t.join()

    assert len(client.calls) == 1
    payload = client.calls[0]["json"]
    assert payload["chat_id"] == "6403706808"
    assert "换到新一段" in payload["text"]
    _assert_no_jargon(payload["text"])


def test_tail_routine_iters_silent(tmp_path: Path) -> None:
    """Many routine rows; nothing pushed (default silent)."""
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("")
    relay, client, _ = _make_relay(tmp_path)

    tailer = otg.OutboxTailer(path=outbox, from_start=False, poll_secs=0.01)

    rows = [
        {"iteration": i, "phase": "healthy", "state": "running",
         "oos_sharpe": 0.05 * (i % 3)}
        for i in range(1, 30)
    ]

    def _producer() -> None:
        time.sleep(0.05)
        with outbox.open("a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        time.sleep(0.2)
        tailer.stop()

    filt = otg.FilterState()
    t = threading.Thread(target=_producer)
    t.start()
    tailer.run(lambda line: otg._handle_line(line, relay, filt, label="sp500-grid"))
    t.join()

    # Could be 0 or up to 1 (the very first row is a "first state transition"
    # in some interpretations, but our FilterState only fires on
    # paused/pending/error/event phases, not running). Expect 0.
    assert len(client.calls) == 0


# --------------------------------------------------------------------------- #
# 4. 4xx → retried 3 times → enqueued
# --------------------------------------------------------------------------- #


def test_telegram_4xx_retries_then_queues(tmp_path: Path) -> None:
    relay, client, sleeps = _make_relay(
        tmp_path,
        responses=[400, 400, 400],
    )
    ok = relay.send("hello")
    assert ok is False
    assert len(client.calls) == 3  # exactly 3 attempts
    # backoff between attempts: only 2 sleeps (after attempt 1 and 2)
    assert len(sleeps) == 2

    queue_path = tmp_path / "retry.jsonl"
    assert queue_path.exists()
    lines = queue_path.read_text().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["text"] == "hello"


# --------------------------------------------------------------------------- #
# 5. Default does NOT replay history
# --------------------------------------------------------------------------- #


def test_existing_lines_not_resent_by_default(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    rows = [
        {"iteration": i, "phase": "pool_attached",
         "pool_id": i, "n_events": 25}
        for i in range(5)
    ]
    outbox.write_text("".join(json.dumps(r) + "\n" for r in rows))

    relay, client, _ = _make_relay(tmp_path)
    tailer = otg.OutboxTailer(path=outbox, from_start=False, poll_secs=0.01)

    def _producer() -> None:
        time.sleep(0.05)
        tailer.stop()

    t = threading.Thread(target=_producer)
    t.start()
    tailer.run(lambda line: otg._handle_line(line, relay, otg.FilterState()))
    t.join()

    assert client.calls == []  # nothing replayed


# --------------------------------------------------------------------------- #
# 6. Missing token env → exit 1, friendly stderr
# --------------------------------------------------------------------------- #


def test_main_missing_token_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("")
    rc = otg.main(
        [
            "--outbox", str(outbox),
            "--chat-id", "6403706808",
            "--token-env", "TELEGRAM_BOT_TOKEN",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err
    assert "missing" in err.lower() or "empty" in err.lower()


# --------------------------------------------------------------------------- #
# 7. Drain queue on startup
# --------------------------------------------------------------------------- #


def test_drain_queue_resends_then_clears(tmp_path: Path) -> None:
    relay, client, _ = _make_relay(tmp_path, responses=[200, 200])
    queue_path = tmp_path / "retry.jsonl"
    queue_path.write_text(
        json.dumps({"text": "msg-1"}) + "\n"
        + json.dumps({"text": "msg-2"}) + "\n"
    )

    n = relay.drain_queue()
    assert n == 2
    assert len(client.calls) == 2
    assert not queue_path.exists()


def test_drain_queue_keeps_failures(tmp_path: Path) -> None:
    # First message: succeeds (1 call). Second: fails 3x → kept in queue.
    relay, client, _ = _make_relay(
        tmp_path, responses=[200, 500, 500, 500],
    )
    queue_path = tmp_path / "retry.jsonl"
    queue_path.write_text(
        json.dumps({"text": "msg-1"}) + "\n"
        + json.dumps({"text": "msg-2"}) + "\n"
    )

    n = relay.drain_queue()
    assert n == 1
    assert queue_path.exists()
    leftover = queue_path.read_text().strip().splitlines()
    assert len(leftover) == 1
    assert json.loads(leftover[0])["text"] == "msg-2"


# --------------------------------------------------------------------------- #
# 8. CLI default flags reflect the silent policy
# --------------------------------------------------------------------------- #


def test_cli_defaults_are_silent_policy() -> None:
    p = otg.build_arg_parser()
    args = p.parse_args(["--outbox", "x", "--chat-id", "1"])
    # heartbeat off by default — no periodic noise.
    assert args.heartbeat_every == 0
    # significant-shift threshold 0.5, not 0.05.
    assert args.oos_delta == 0.5
