"""HTTP-level tests for the Hermes run path on the Workbench.

The native run path (``POST /api/run``) is covered by ``test_ui_api.py``. This
file covers the *external* path: ``POST /api/run-hermes``, which spawns the
Hermes Agent as a text-only subprocess. What is under test here is not Hermes --
there is no network, no process, and no credential in any of these tests -- but
the loop Forge owns around it: the route's guards, the run id, the
``hermes_result`` that ``GET /api/status`` exposes after completion, and the
fact that no profile travels from the request to the child -- the profile is a
fixed adapter decision, so a request cannot name one.

The subprocess itself is faked by monkeypatching ``forge.ui.runner.spawn``, so
no test here opens a socket or starts a child. The adapter's argv and
environment are the real ones, asserted in ``test_external_hermes.py``; here
they are assumed correct and the composition is what is exercised.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.external.base import ExternalResult
from forge.external.runner import ExternalRun
from forge.models.scripted import ScriptedGateway
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.runner import build_hermes_runner, runtime_factory
from forge.ui.security import TOKEN_HEADER, UiToken
from forge.ui.server import create_app
from forge.ui.session import WorkbenchSession
from forge.ui.selector import ModelSelection, ModelSelector

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


def _canned_result(**overrides: object) -> ExternalResult:
    fields: dict[str, object] = {
        "agent": "hermes",
        "text": "the answer",
        "ok": True,
        "exit_code": 0,
        "model": "scripted/hermes-model",
        "provider": "scripted",
        "session_id": "sess-0001",
    }
    fields.update(overrides)
    return ExternalResult(**fields)  # type: ignore[arg-type]


class HermesHarness:
    """One app wired with a Hermes runner and a faked spawn."""

    def __init__(
        self,
        workspace: Workspace,
        result: ExternalResult,
        *,
        launch_error: str = "",
        timed_out: bool = False,
        recorded: list,
    ) -> None:
        self.token = UiToken()
        self.model = ScriptedGateway([])
        self.broker = SseBroker()
        self.recorded = recorded

        self.session = WorkbenchSession(
            workspace=workspace,
            profile=BYTE,
            selector=ModelSelector({}, ModelSelection("openrouter", "scripted")),
            runtime_factory=runtime_factory(
                workspace, profile=BYTE, gateway=self.model, model_id="scripted"
            ),
            broker=self.broker,
            hermes_runner=build_hermes_runner(workspace),
        )
        self.app = create_app(
            workspace=workspace,
            session=self.session,
            gateway=self.model,
            token=self.token,
            port=PORT,
        )
        self.client = TestClient(self.app)
        self._result = result
        self._launch_error = launch_error
        self._timed_out = timed_out

    def headers(self, *, token: str | None = None, origin: str | None = ORIGIN):
        headers: dict[str, str] = {}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers[TOKEN_HEADER] = token
        return headers

    def fake_spawn(self, adapter, invocation):
        self.recorded.append(invocation)
        return ExternalRun(
            result=self._result,
            timed_out=self._timed_out,
            launch_error=self._launch_error,
        )

    def _wait_for_terminal(self, timeout: float = 5.0) -> dict[str, object]:
        """Poll ``/api/status`` until the run leaves RUNNING or the budget ends."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.client.get(
                "/api/status", headers=self.headers(token=self.token.value)
            ).json()
            if status["state"] in ("completed", "failed"):
                return status
            time.sleep(0.01)
        raise AssertionError("run did not reach a terminal state in time")


def test_run_hermes_returns_202_and_status_reflects_the_result(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: ``202`` + run id, and ``/api/status`` carries ``hermes_result``.

    The spawn is faked, so the loop is tested end to end without a subprocess:
    submit, the background thread, the result landing on the record, and the
    status route reading it back.
    """
    recorded: list = []
    harness = HermesHarness(
        workspace, _canned_result(), recorded=recorded
    )
    monkeypatch.setattr("forge.ui.runner.spawn", harness.fake_spawn)

    response = harness.client.post(
        "/api/run-hermes",
        json={"task": "summarise this"},
        headers=harness.headers(token=harness.token.value),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"]

    status = harness._wait_for_terminal()
    assert status["state"] == "completed"
    assert status["hermes_result"] == {
        "text": "the answer",
        "model": "scripted/hermes-model",
        "session_id": "sess-0001",
        "ok": True,
    }

    # The invocation Forge built carries no profile: the profile is a fixed
    # adapter decision, never a value a caller can name.
    assert len(recorded) == 1
    assert not hasattr(recorded[0], "profile")
    assert recorded[0].agent == "hermes"

    # The result event reached the broker with the fields the page renders.
    replayed, _ = harness.broker.replay_since(0)
    result_events = [e for _, e in replayed if e.name == "hermes.result"]
    assert len(result_events) == 1
    assert result_events[0].get("text") == "the answer"
    assert result_events[0].get("model") == "scripted/hermes-model"
    assert result_events[0].get("session_id") == "sess-0001"


def test_run_hermes_ignores_an_unexpected_profile_field(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request cannot name a profile, even by smuggling one in.

    ``RunHermesRequest`` has no profile field and ``ExternalInvocation`` has
    none either, so a body carrying ``"profile": "default"`` is inert data --
    it can neither widen nor change the fixed text-only profile the adapter
    runs under.
    """
    recorded: list = []
    harness = HermesHarness(workspace, _canned_result(), recorded=recorded)
    monkeypatch.setattr("forge.ui.runner.spawn", harness.fake_spawn)

    response = harness.client.post(
        "/api/run-hermes",
        json={"task": "summarise this", "profile": "default"},
        headers=harness.headers(token=harness.token.value),
    )
    assert response.status_code == 202
    harness._wait_for_terminal()

    assert len(recorded) == 1
    assert not hasattr(recorded[0], "profile")


def test_run_hermes_refuses_without_a_token(workspace: Workspace) -> None:
    """The Hermes route takes the same guards as every other command route."""
    harness = HermesHarness(workspace, _canned_result(), recorded=[])

    response = harness.client.post(
        "/api/run-hermes",
        json={"task": "summarise this"},
        headers=harness.headers(token=None),
    )

    assert response.status_code == 403


def test_run_hermes_refuses_an_empty_task(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty task is a 422 before anything starts."""
    harness = HermesHarness(workspace, _canned_result(), recorded=[])
    monkeypatch.setattr("forge.ui.runner.spawn", harness.fake_spawn)

    response = harness.client.post(
        "/api/run-hermes",
        json={"task": "   "},
        headers=harness.headers(token=harness.token.value),
    )

    assert response.status_code == 422


def test_run_hermes_is_a_conflict_while_a_run_is_in_flight(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One process, one run -- the Hermes path shares the rule with the native one.

    The first faked spawn blocks until released, so the second submit collides
    with a live worker thread and is refused with 409 rather than queued.
    """
    recorded: list = []
    harness = HermesHarness(workspace, _canned_result(), recorded=recorded)

    release = {"go": False}

    def blocking_spawn(adapter, invocation):
        recorded.append(invocation)
        while not release["go"]:
            time.sleep(0.01)
        return ExternalRun(result=_canned_result())

    monkeypatch.setattr("forge.ui.runner.spawn", blocking_spawn)

    first = harness.client.post(
        "/api/run-hermes",
        json={"task": "first task"},
        headers=harness.headers(token=harness.token.value),
    )
    assert first.status_code == 202

    second = harness.client.post(
        "/api/run-hermes",
        json={"task": "second task"},
        headers=harness.headers(token=harness.token.value),
    )
    assert second.status_code == 409

    release["go"] = True
    harness._wait_for_terminal()
