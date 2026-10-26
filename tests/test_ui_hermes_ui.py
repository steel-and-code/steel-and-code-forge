"""Frontend regression coverage for the Byte/Hermes run-mode selector (FORGE-20).

The backend path -- ``POST /api/run-hermes``, the ``hermes_result`` status field,
and the shared state machine -- is covered by ``test_ui_hermes_api.py`` at the
HTTP layer. This file covers the *page*: that a send with the selector on
"hermes" actually posts to ``/api/run-hermes`` and not ``/api/run``, that a
completed Hermes run renders its answer into the conversation from
``/api/status`` (``hermes_result.text``), and that a failed Hermes run states
the failure rather than fabricating an answer.

The page half is driven by ``tests/ui_probe/hermes_probe.js``, which loads the
shipped ``app.js`` into a stub DOM exactly as ``test_ui_final_answer.py`` does,
because app.js is an IIFE with no module boundary -- the only way to assert what
the file that ships actually does is to run it. Asserting on a hand-copied
router would prove nothing about the page an operator uses.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
APP_JS = REPO / "forge" / "ui" / "static" / "app.js"
INDEX_HTML = REPO / "forge" / "ui" / "static" / "index.html"
PROBE = pathlib.Path(__file__).resolve().parent / "ui_probe" / "hermes_probe.js"

NODE = shutil.which("node")

requires_node = pytest.mark.skipif(
    NODE is None,
    reason="node is required to drive the shipped app.js against a stub DOM",
)

ANSWER = "the hermes answer, in full"


def _run_page(steps: list[dict]) -> dict:
    """Drive the shipped ``app.js`` through a scripted sequence."""
    result = subprocess.run(
        [NODE, str(PROBE), json.dumps(steps)],
        capture_output=True,
        text=True,
        env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assistant(entries: list[dict]) -> list[str]:
    return [entry["text"] for entry in entries if entry["role"] == "assistant"]


def _hermes_status(text: str, run_id: str = "run-hermes-0001") -> dict:
    return {
        "state": "completed",
        "run_id": run_id,
        "workspace": "/tmp/ws",
        "agent": "byte",
        "model": "scripted",
        "pending_approval": None,
        "hermes_result": {
            "text": text,
            "model": "scripted/hermes-model",
            "session_id": "sess-1",
            "ok": True,
        },
    }


# --------------------------------------------------------------------------
# Routing: the selector chooses the endpoint
# --------------------------------------------------------------------------


@requires_node
def test_a_hermes_send_posts_to_run_hermes() -> None:
    """With the selector on "hermes", a send hits ``/api/run-hermes``."""

    page = _run_page(
        [
            {"mode": "hermes", "task": "explain the cache", "submit": True},
        ]
    )

    assert page["runs"] == ["/api/run-hermes"], (
        "a Hermes send must post to /api/run-hermes; got " f"{page['runs']}"
    )


@requires_node
def test_a_byte_send_posts_to_run() -> None:
    """The Byte default is unchanged: a send posts to ``/api/run``."""

    page = _run_page(
        [
            {"mode": "byte", "task": "explain the cache", "submit": True},
        ]
    )

    assert page["runs"] == ["/api/run"], (
        "a Byte send must post to /api/run; got " f"{page['runs']}"
    )


@requires_node
def test_the_default_with_no_selector_still_posts_to_run() -> None:
    """A page whose selector never changes the default stays on ``/api/run``."""

    page = _run_page(
        [
            {"task": "explain the cache", "submit": True},
        ]
    )

    assert page["runs"] == ["/api/run"]


# --------------------------------------------------------------------------
# The Hermes answer reaches the conversation
# --------------------------------------------------------------------------


@requires_node
def test_a_completed_hermes_run_renders_its_answer_once() -> None:
    """``hermes_result.text`` is written into the conversation, exactly once."""

    page = _run_page(
        [
            {"mode": "hermes", "task": "explain the cache", "submit": True},
            {"status": _hermes_status(ANSWER)},
            {"event": "session.state", "data": {"state": "completed"}},
        ]
    )

    assert _assistant(page["entries"]) == [ANSWER], (
        "the completed Hermes run must render its answer once; got "
        f"{_assistant(page['entries'])}"
    )


@requires_node
def test_a_failed_hermes_run_states_the_failure_without_an_answer() -> None:
    """A failed run shows the reason and fabricates no assistant answer."""

    page = _run_page(
        [
            {"mode": "hermes", "task": "explain the cache", "submit": True},
            {"event": "hermes.failed", "data": {"detail": "child exited badly"}},
        ]
    )

    assert _assistant(page["entries"]) == [], (
        "a failed Hermes run must add no assistant entry"
    )
    assert any(
        entry["role"] == "system" and "child exited badly" in entry["text"]
        for entry in page["entries"]
    ), "the failure itself must be shown"


# --------------------------------------------------------------------------
# Structural guards on the shipped files
# --------------------------------------------------------------------------


def test_the_page_carries_the_run_mode_selector() -> None:
    """The selector exists, with the two run paths it is named for."""

    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="run-mode"' in html
    assert 'value="byte"' in html
    assert 'value="hermes"' in html


def test_app_js_reads_the_hermes_answer_from_status() -> None:
    """The Hermes answer is recovered from ``/api/status``, never from the event.

    The ``hermes.result`` stream frame carries the answer *clipped* by the same
    field clamp that clips ``model.reply``; the authoritative text lives under
    ``hermes_result`` in the status read. Asserted on the source so the recovery
    stays a read of server state rather than a second rendering path keyed on a
    stream event.
    """

    source = APP_JS.read_text(encoding="utf-8")

    assert "status.hermes_result" in source
    assert '"/api/run-hermes"' in source
    # The page still recovers the native answer the same way it always did.
    assert "status.result" in source