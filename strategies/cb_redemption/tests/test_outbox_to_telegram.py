"""Tests for ``scripts/outbox_to_telegram.py`` relay.

All tests stub httpx and use ``tmp_path`` for outbox + retry queue. None hit
the real Telegram API.
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
# 1. Format helpers
# --------------------------------------------------------------------------- #


def test_format_message_basic() -> None:
    row = {
        "iteration": 5,
        "verdict": "accepted",
        "oos_sharpe": 1.234,
        "change_summary": "bumped w_redeem",
        "state": "running",
    }
    msg = otg.format_message(row)
    assert "iter=5" in msg
    assert "accepted" in msg
    assert "OOS=1.23" in msg
    assert "change: bumped w_redeem" in msg
    assert "state: running" in msg
    assert not msg.startswith("❗")


def test_format_message_paused_gets_bang() -> None:
    row = {
        "iteration": 9,
        "verdict": "paused",
        "oos_sharpe": None,
        "state": "paused",
        "paused_reason": "max_recovery_attempts",
    }
    msg = otg.format_message(row)
    assert msg.startswith("❗")
    assert "OOS=n/a" in msg
    assert "max_recovery_attempts" in msg


# --------------------------------------------------------------------------- #
# 2. Happy path: one new line → one telegram call
# --------------------------------------------------------------------------- #


def test_tail_single_new_line_sends_once(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("")  # exists, empty
    relay, client, _ = _make_relay(tmp_path)

    tailer = otg.OutboxTailer(
        path=outbox, from_start=False, poll_secs=0.01,
    )

    # Append one row from another thread, then stop the tailer.
    row = {
        "iteration": 1,
        "verdict": "accepted",
        "oos_sharpe": 0.5,
        "change_summary": "x",
        "state": "running",
    }

    def _producer() -> None:
        time.sleep(0.05)
        with outbox.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        time.sleep(0.1)
        tailer.stop()

    t = threading.Thread(target=_producer)
    t.start()
    tailer.run(lambda line: otg._handle_line(line, relay))
    t.join()

    assert len(client.calls) == 1
    payload = client.calls[0]["json"]
    assert payload["chat_id"] == "6403706808"
    assert "iter=1" in payload["text"]
    assert "accepted" in payload["text"]


# --------------------------------------------------------------------------- #
# 3. 4xx → retried 3 times → enqueued
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
# 4. Default does NOT replay history; --from-start does
# --------------------------------------------------------------------------- #


def test_existing_lines_not_resent_by_default(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    rows = [
        {"iteration": i, "verdict": "v", "oos_sharpe": 0.0,
         "change_summary": "", "state": "running"}
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
    tailer.run(lambda line: otg._handle_line(line, relay))
    t.join()

    assert client.calls == []  # nothing replayed


def test_from_start_resends_history(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    rows = [
        {"iteration": i, "verdict": "v", "oos_sharpe": 0.0,
         "change_summary": "", "state": "running"}
        for i in range(5)
    ]
    outbox.write_text("".join(json.dumps(r) + "\n" for r in rows))

    relay, client, _ = _make_relay(tmp_path)
    tailer = otg.OutboxTailer(path=outbox, from_start=True, poll_secs=0.01)

    def _producer() -> None:
        time.sleep(0.1)
        tailer.stop()

    t = threading.Thread(target=_producer)
    t.start()
    tailer.run(lambda line: otg._handle_line(line, relay))
    t.join()

    assert len(client.calls) == 5
    iters = [
        json.loads(c["json"]["text"].split("\n")[0]
                   .split("|")[0].split("=")[1])
        for c in client.calls
    ]
    assert iters == [0, 1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# 5. Multi-line append preserves order
# --------------------------------------------------------------------------- #


def test_multiple_appended_lines_in_order(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("")
    relay, client, _ = _make_relay(tmp_path)

    tailer = otg.OutboxTailer(path=outbox, from_start=False, poll_secs=0.01)

    rows = [
        {"iteration": i, "verdict": "v", "oos_sharpe": float(i),
         "change_summary": f"c{i}", "state": "running"}
        for i in range(4)
    ]

    def _producer() -> None:
        time.sleep(0.05)
        with outbox.open("a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        time.sleep(0.1)
        tailer.stop()

    t = threading.Thread(target=_producer)
    t.start()
    tailer.run(lambda line: otg._handle_line(line, relay))
    t.join()

    assert len(client.calls) == 4
    texts = [c["json"]["text"] for c in client.calls]
    for i, txt in enumerate(texts):
        assert f"iter={i}" in txt


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
