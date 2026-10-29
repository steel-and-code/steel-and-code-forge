"""The Workbench model-selection surface: binding, guards, and offline behaviour.

The routes under test are the thin HTTP edge of :class:`forge.ui.selector.
ModelSelector`, and the properties that matter are the same ones the selector
holds at its boundary:

**Provider and model travel as one binding.** ``POST /api/selection`` applies
them together; there is no route that sets a model without a provider, and the
response -- like ``GET /api/status`` afterwards -- reports the pair the server
actually holds, so a client can never believe it routed a model one place while
the server sent it another.

**A read never probes.** ``GET /api/selection`` returns cached state. Only the
explicit ``POST /api/selection/refresh`` contacts a provider, and it is refused
while a run is in flight.

**An unavailable provider is an error, never a fallback.** When the local
provider's listing fails, the snapshot reports ``available=False`` with a reason
and the selection stays put -- the run still goes through the local gateway
rather than silently switching to the paid host.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.models.base import ModelListing
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.runner import runtime_factory
from forge.ui.security import SEC_FETCH_SITE_HEADER, TOKEN_HEADER, UiToken
from forge.ui.server import create_app
from forge.ui.session import UiSessionState, WorkbenchSession
from forge.ui.selector import ModelSelection, ModelSelector

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


class Harness:
    """One app, one session, one token, one selector."""

    def __init__(self, workspace, *, selection=None, turns=()) -> None:
        self.token = UiToken()
        self.selector = ModelSelector(
            {}, selection if selection is not None else ModelSelection("openrouter", "vendor/coder")
        )
        self.model = ScriptedGateway(list(turns))
        self.session = WorkbenchSession(
            workspace=workspace,
            profile=BYTE,
            selector=self.selector,
            runtime_factory=runtime_factory(
                workspace, profile=BYTE, gateway=self.model, model_id="vendor/coder"
            ),
            broker=SseBroker(),
        )
        self.app = create_app(
            workspace=workspace,
            session=self.session,
            gateway=self.model,
            token=self.token,
            port=PORT,
        )
        self.client = TestClient(self.app)

    def headers(self, *, token=None, origin=ORIGIN, **extra):
        headers = dict(extra)
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers[TOKEN_HEADER] = token
        return headers

    def auth(self, token=None, **overrides):
        return self.headers(token=self.token.value if token is None else token, **overrides)

    def read_selection(self, **overrides):
        return self.client.get("/api/selection", headers=self.auth(**overrides))

    def set_selection(self, provider, model, **overrides):
        return self.client.post(
            "/api/selection", json={"provider": provider, "model": model}, headers=self.auth(**overrides)
        )

    def refresh_selection(self, **overrides):
        return self.client.post("/api/selection/refresh", headers=self.auth(**overrides))

    def status(self):
        return self.client.get("/api/status", headers=self.auth())

    def submit(self, task="do a thing"):
        return self.client.post("/api/run", json={"task": task}, headers=self.auth())

    def wait_pending(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = self.session.channel.pending
            if pending is not None:
                return pending
            time.sleep(0.01)
        return None

    def wait_state(self, state: UiSessionState, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session.state is state:
                return True
            time.sleep(0.01)
        return self.session.state is state


@pytest.fixture()
def harness(workspace: Workspace) -> Harness:
    return Harness(workspace)


GATED_TURNS = [
    ScriptedTurn(
        text="I will remove the build directory.",
        tool_calls=(("exec_command", {"program": "rm", "args": ["-rf", "build"]}),),
    ),
    ScriptedTurn(text="Understood, I did not run it."),
]


# --------------------------------------------------------------------------
# The read path: cached, never probing
# --------------------------------------------------------------------------


def test_the_selection_read_reports_the_selected_pair(harness) -> None:
    body = harness.read_selection().json()

    assert body["selected"] == {"provider": "openrouter", "model": "vendor/coder"}
    assert body["providers"] == ["local", "openrouter"]


def test_the_status_read_reports_provider_and_model(harness) -> None:
    body = harness.status().json()

    assert body["provider"] == "openrouter"
    assert body["model"] == "vendor/coder"


def test_a_read_never_pops_providers(harness, monkeypatch) -> None:
    from forge.models import verify as verify_module

    probed: list[str] = []

    def spy(provider, *, environ, timeout=None):
        probed.append(provider)
        return ModelListing(provider=provider, models=(), available=True)

    monkeypatch.setattr(verify_module, "list_provider_models", spy)

    for _ in range(3):
        assert harness.read_selection().status_code == 200

    assert probed == [], "GET /api/selection must not contact a provider"


# --------------------------------------------------------------------------
# Binding: provider + model together
# --------------------------------------------------------------------------


def test_setting_the_selection_binds_provider_and_model_together(harness) -> None:
    response = harness.set_selection("local", "qwen3.5:27b-q4_K_M")

    assert response.status_code == 200
    assert response.json()["selected"] == {"provider": "local", "model": "qwen3.5:27b-q4_K_M"}

    # The authoritative status read agrees with the snapshot.
    status = harness.status().json()
    assert status["provider"] == "local"
    assert status["model"] == "qwen3.5:27b-q4_K_M"


def test_an_unknown_provider_is_refused(harness) -> None:
    assert harness.set_selection("anthropic", "claude").status_code == 422


def test_an_empty_model_is_refused(harness) -> None:
    assert harness.set_selection("local", "").status_code == 422


def test_a_missing_model_field_is_refused_by_the_schema(harness) -> None:
    response = harness.client.post(
        "/api/selection", json={"provider": "local"}, headers=harness.auth()
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Capability guards (same rules as every other command route)
# --------------------------------------------------------------------------


def test_setting_the_selection_requires_a_token(harness) -> None:
    response = harness.client.post(
        "/api/selection",
        json={"provider": "local", "model": "x"},
        headers=harness.headers(),
    )
    assert response.status_code == 403


def test_setting_the_selection_requires_an_exact_origin(harness) -> None:
    assert harness.set_selection("local", "x", origin="http://evil.example").status_code == 403
    assert (
        harness.set_selection("local", "x", origin=None, **{SEC_FETCH_SITE_HEADER: "same-origin"}).status_code
        == 403
    )


def test_reading_the_selection_requires_a_token(harness) -> None:
    assert harness.client.get("/api/selection", headers=harness.headers()).status_code == 403


# --------------------------------------------------------------------------
# Changes apply between runs, never during one
# --------------------------------------------------------------------------


def test_a_selection_change_is_refused_while_a_run_is_in_flight(harness) -> None:
    harness = Harness(harness.session.workspace, turns=GATED_TURNS)
    assert harness.submit("clean up").status_code == 202
    assert harness.wait_pending() is not None

    response = harness.set_selection("local", "qwen3.5:27b-q4_K_M")

    assert response.status_code == 409
    # The selection did not move.
    assert harness.selector.selection.provider == "openrouter"


def test_a_selection_change_applies_to_the_next_run(harness) -> None:
    turns = [ScriptedTurn(text="done")]
    harness = Harness(harness.session.workspace, turns=turns)

    harness.set_selection("local", "qwen3.5:27b-q4_K_M")
    assert harness.submit("task").status_code == 202
    assert harness.wait_state(UiSessionState.COMPLETED)

    # The completed run's status still reports the selection it used.
    assert harness.status().json()["provider"] == "local"


# --------------------------------------------------------------------------
# Offline behaviour: an unavailable provider is an error, never a fallback
# --------------------------------------------------------------------------


def test_an_unavailable_local_provider_is_reported_not_fallen_back(harness, monkeypatch) -> None:
    from forge.models import verify as verify_module

    def offline(provider, *, environ, timeout=None):
        if provider == "local":
            return ModelListing(provider="local", available=False, error="ConnectionRefusedError")
        return ModelListing(provider="openrouter", models=("vendor/coder",), available=True)

    monkeypatch.setattr(verify_module, "list_provider_models", offline)

    harness.set_selection("local", "qwen3.5:27b-q4_K_M")

    body = harness.read_selection().json()
    assert body["available"] == {}, "refresh has not run yet on this harness"

    refreshed = harness.refresh_selection().json()

    # The unavailable local provider is reported with a reason ...
    assert refreshed["available"]["local"]["available"] is False
    assert "Connection" in refreshed["available"]["local"]["error"]
    # ... and the selection has not silently moved to the paid host.
    assert refreshed["selected"]["provider"] == "local"
    assert refreshed["selected"]["model"] == "qwen3.5:27b-q4_K_M"


def test_refresh_is_refused_while_a_run_is_in_flight(harness) -> None:
    harness = Harness(harness.session.workspace, turns=GATED_TURNS)
    assert harness.submit("clean up").status_code == 202
    assert harness.wait_pending() is not None

    assert harness.refresh_selection().status_code == 409


def test_a_selection_response_carries_no_credential(harness) -> None:
    # The selector holds only the (provider, model) and the catalog; no key
    # can appear in its published snapshot.
    body = harness.read_selection().text
    assert "FORGE_OPENROUTER_API_KEY" not in body
    assert "sk-" not in body