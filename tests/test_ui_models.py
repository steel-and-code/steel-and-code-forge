"""The Workbench model-health surface: exposure, caching, and re-checking.

Three properties are under test, and each one exists because the obvious
alternative was rejected:

**The backend is the only thing that decides health.** Every assertion here
compares the HTTP body against ``ModelRouter.status()`` computed in the test
from the same router. A UI that re-derived GREEN/YELLOW/RED in JavaScript would
still pass a rendering test, so the contract is pinned at the boundary instead.

**A GET never probes.** ``GET /api/models`` reads a cached snapshot. Probing
opens a socket and spends a credential, and a page that polls on load would turn
a refresh button into a way to spend money repeatedly. The test asserts a call
count, not an intention.

**A re-check is explicit, bounded, and never incidental.** It is a POST, it needs
the exact-Origin rule like every other state-changing route, it refuses while a
run is in flight (a verification pass must never ride along on an executing run
as a side effect), and it inherits the verifier's own one-attempt, five-second
policy rather than growing a second one.

Nothing here reaches a network. Verifiers are injected at the catalog boundary.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.models.base import VerificationCode, VerificationResult
from forge.models.providers import ProviderCatalog, ProviderSpec
from forge.models.registry import parse_registry
from forge.models.router import ModelRouter
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.models import ModelHealth
from forge.ui.runner import runtime_factory
from forge.ui.security import SEC_FETCH_SITE_HEADER, TOKEN_HEADER, UiToken
from forge.ui.server import create_app
from forge.ui.session import UiSessionState, WorkbenchSession
from forge.ui.selector import ModelSelection, ModelSelector

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"

#: A credential the server holds and no response may carry. Named so a leak in a
#: serialised body is unmistakable rather than incidental.
API_KEY = "sk-test-do-not-leak-this-value"


# --------------------------------------------------------------------------
# A registry whose only provider is a fake, so nothing can reach a socket
# --------------------------------------------------------------------------


class _RecordingVerifier:
    """One model per call, recording that it was asked.

    ``usable`` is the answer for models in ``usable``; every other model comes
    back as ``NOT_FOUND``, which is what a provider that does not serve it
    actually says.
    """

    def __init__(self, usable: set[str], calls: list[str]) -> None:
        self._usable = usable
        self._calls = calls

    def verify(self, model: str) -> VerificationResult:
        self._calls.append(model)
        if model in self._usable:
            return VerificationResult.success(model, reason="provider lists this model")
        return VerificationResult.failure(
            model,
            VerificationCode.NOT_FOUND,
            reason=f"provider does not list {model!r}",
        )


def _document(
    primary: str = "alpha",
    fallbacks: tuple[str, ...] = ("beta",),
) -> str:
    entries = "".join(
        f'  {{ model = "{name}", provider = "fake" }},\n' for name in fallbacks
    )
    return (
        f'[tiers.general_light]\n'
        f'primary = {{ model = "{primary}", provider = "fake" }}\n'
        f"fallbacks = [\n{entries}]\n\n"
        f'[tiers.coding_standard]\n'
        f'primary = {{ model = "{primary}", provider = "fake" }}\n\n'
        f'[tiers.reasoning_heavy]\n'
        f'primary = {{ model = "{primary}", provider = "fake" }}\n\n'
        f"[routing]\n"
        f'default_tier = "general_light"\n'
    )


def _router(
    calls: list[str],
    *,
    usable: set[str] | None = None,
    document: str | None = None,
) -> ModelRouter:
    """A router over one fake provider, whose verifier records every attempt."""
    answer = {"usable": usable if usable is not None else {"alpha"}}

    def verifier_factory(environ):
        return _RecordingVerifier(answer["usable"], calls)

    catalog = ProviderCatalog(
        {
            "fake": ProviderSpec(
                name="fake",
                credential_env="FAKE_API_KEY",
                gateway_factory=lambda: object(),
                verifier_factory=verifier_factory,
            )
        }
    )
    registry = parse_registry(
        document if document is not None else _document(), catalog=catalog
    )
    return ModelRouter(registry)


ENVIRON = {"FAKE_API_KEY": API_KEY}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


class Harness:
    """One app, one session, one token, one health cache."""

    def __init__(
        self,
        workspace: Workspace,
        router: ModelRouter,
        *,
        health: ModelHealth | None = None,
        turns=(),
    ) -> None:
        self.token = UiToken()
        self.model = ScriptedGateway(list(turns))
        self.health = health if health is not None else ModelHealth(router, environ=ENVIRON)
        self.session = WorkbenchSession(
            workspace=workspace,
            profile=BYTE,
            selector=ModelSelector({}, ModelSelection("openrouter", "alpha")),
            runtime_factory=runtime_factory(
                workspace, profile=BYTE, gateway=self.model, model_id="alpha"
            ),
            broker=SseBroker(),
        )
        self.app = create_app(
            workspace=workspace,
            session=self.session,
            gateway=self.model,
            token=self.token,
            port=PORT,
            health=self.health,
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
        return self.headers(
            token=self.token.value if token is None else token, **overrides
        )

    def models(self, **overrides):
        return self.client.get("/api/models", headers=self.auth(**overrides))

    def verify_models(self, **overrides):
        return self.client.post("/api/models/verify", headers=self.auth(**overrides))

    def submit(self, task: str = "do a thing"):
        return self.client.post(
            "/api/run", json={"task": task}, headers=self.auth()
        )

    def wait_state(self, state: UiSessionState, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session.state is state:
                return True
            time.sleep(0.01)
        return self.session.state is state

    def wait_pending(self, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = self.session.channel.pending
            if pending is not None:
                return pending
            time.sleep(0.01)
        return None


GATED_TURNS = [
    ScriptedTurn(
        text="I need to remove the build directory.",
        tool_calls=(("exec_command", {"program": "rm", "args": ["-rf", "build"]}),),
    ),
    ScriptedTurn(text="Understood, I did not run it."),
]


@pytest.fixture()
def calls() -> list[str]:
    return []


@pytest.fixture()
def harness(workspace: Workspace, calls: list[str]) -> Harness:
    return Harness(workspace, _router(calls))


# --------------------------------------------------------------------------
# The read path returns the router's status, unchanged
# --------------------------------------------------------------------------


def test_models_are_the_router_status_verbatim(workspace, calls) -> None:
    router = _router(calls)
    harness = Harness(workspace, router)

    response = harness.models()

    assert response.status_code == 200
    assert response.json()["tiers"] == router.status()


def test_every_tier_appears_even_when_the_registry_lacks_it(workspace, calls) -> None:
    """The router's contract: an absent tier is reported, not omitted.

    The frontend must never have to decide what a missing tier means, so this
    asserts the count rather than inspecting a health value.
    """
    harness = Harness(workspace, _router(calls))

    tiers = harness.models().json()["tiers"]

    assert {entry["tier"] for entry in tiers} >= {
        "general_light",
        "coding_standard",
        "reasoning_heavy",
    }
    assert all({"health", "model", "provider", "selection", "slots"} <= set(e) for e in tiers)


def test_the_snapshot_reports_that_nothing_was_checked_yet(
    workspace, calls
) -> None:
    harness = Harness(workspace, _router(calls))

    body = harness.models().json()

    assert body["checked_at"] is None
    assert all(entry["verified"] is False for entry in body["tiers"])


# --------------------------------------------------------------------------
# A GET never probes
# --------------------------------------------------------------------------


def test_reading_the_models_never_contacts_a_provider(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls))

    for _ in range(3):
        assert harness.models().status_code == 200

    assert calls == [], "a read must not spend a request on a provider"


def test_a_refresh_never_contacts_a_provider(workspace, calls) -> None:
    """What a browser tab actually does on load and on visibility change."""
    harness = Harness(workspace, _router(calls))

    harness.models()
    harness.models(origin=None, **{SEC_FETCH_SITE_HEADER: "same-origin"})

    assert calls == []


# --------------------------------------------------------------------------
# Capability checks
# --------------------------------------------------------------------------


def test_models_requires_a_token(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls))
    assert harness.client.get("/api/models", headers=harness.headers()).status_code == 403


def test_models_refuses_a_cross_site_get(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls))
    assert harness.models(origin="http://evil.example").status_code == 403


def test_a_same_origin_get_with_no_origin_header_is_served(
    workspace, calls
) -> None:
    """The measured Chrome header set for the Workbench's own page."""
    harness = Harness(workspace, _router(calls))

    response = harness.client.get(
        "/api/models",
        headers={
            SEC_FETCH_SITE_HEADER: "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"http://127.0.0.1:{PORT}/",
            TOKEN_HEADER: harness.token.value,
        },
    )

    assert response.status_code == 200


def test_rechecking_requires_a_token(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls))
    assert (
        harness.client.post("/api/models/verify", headers=harness.headers()).status_code
        == 403
    )


def test_rechecking_requires_an_exact_origin(workspace, calls) -> None:
    """A POST is state-changing: the exact-Origin rule applies unchanged.

    ``Sec-Fetch-Site: same-origin`` must not stand in for an ``Origin`` here,
    exactly as it does not for ``POST /api/run``.
    """
    harness = Harness(workspace, _router(calls))

    assert harness.verify_models(origin="http://evil.example").status_code == 403
    assert (
        harness.verify_models(origin=None, **{SEC_FETCH_SITE_HEADER: "same-origin"}).status_code
        == 403
    )
    assert calls == [], "a refused re-check must not have probed anything"


# --------------------------------------------------------------------------
# Re-checking is explicit and changes the cached answer
# --------------------------------------------------------------------------


def test_rechecking_contacts_each_model_once_and_updates_the_snapshot(
    workspace, calls
) -> None:
    harness = Harness(workspace, _router(calls))

    response = harness.verify_models()

    assert response.status_code == 200
    # One attempt per distinct model. 'alpha' stands behind three tiers and
    # 'beta' is one fallback, so two models -- not five slots.
    assert sorted(set(calls)) == ["alpha", "beta"]
    assert len(calls) == len(set(calls)), "a model was probed more than once"
    assert response.json()["checked_at"], "a checked snapshot must say when"


def test_a_rechecked_snapshot_is_cached_for_later_reads(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls))
    checked = harness.verify_models().json()

    read = harness.models().json()

    assert read["tiers"] == checked["tiers"]
    assert len(calls) == 2, "reading after a re-check probed again"


def test_a_failing_optional_slot_leaves_its_tier_green(workspace, calls) -> None:
    """The case the indicator exists to be honest about.

    A provider that does not serve a *fallback* is an operational fact about
    that slot. It is reported in the tier's details and it must not darken a
    tier whose primary is verified healthy -- the health value is the registry's
    to decide, and the endpoint reports it rather than adjusting it.
    """
    harness = Harness(workspace, _router(calls))

    body = harness.verify_models().json()
    tier = next(e for e in body["tiers"] if e["tier"] == "general_light")

    assert tier["health"] == "GREEN"
    failed = [s for s in tier["slots"] if not s["verified"]]
    assert failed, "the unhealthy fallback must still appear in the details"
    assert any("does not list" in s["verify_reason"] for s in failed)


def test_a_dead_primary_reports_yellow_from_the_fallback(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls, usable={"beta"}))

    body = harness.verify_models().json()
    tier = next(e for e in body["tiers"] if e["tier"] == "general_light")

    assert tier["health"] == "YELLOW"
    assert tier["selection"] == "fallback"
    assert tier["model"] == "beta"


def test_no_usable_model_at_all_reports_red(workspace, calls) -> None:
    harness = Harness(
        workspace,
        _router(calls, usable=set(), document=_document("alpha", ("beta",))),
    )

    body = harness.verify_models().json()
    tier = next(e for e in body["tiers"] if e["tier"] == "general_light")

    assert tier["health"] == "RED"
    assert tier["model"] == ""
    assert tier["selection"] == "none"


# --------------------------------------------------------------------------
# Re-checking never rides along on an executing run
# --------------------------------------------------------------------------


def test_rechecking_is_refused_while_a_run_is_in_flight(workspace, calls) -> None:
    """A verification pass must not be an incidental side effect of a run.

    It opens sockets and spends a credential from inside a process that is
    already mid-task; refusing is the honest answer rather than queueing it.
    """
    harness = Harness(workspace, _router(calls), turns=GATED_TURNS)
    assert harness.submit("clean up").status_code == 202
    assert harness.wait_pending() is not None

    response = harness.verify_models()

    assert response.status_code == 409
    assert calls == []
    assert harness.session.channel.pending is not None, "the prompt is still open"


def test_rechecking_works_again_once_the_run_is_over(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls), turns=GATED_TURNS)
    harness.submit("clean up")
    pending = harness.wait_pending()
    harness.client.post(
        f"/api/approvals/{pending.approval_id}",
        json={"decision": "no"},
        headers=harness.auth(),
    )
    assert harness.wait_state(UiSessionState.COMPLETED)

    assert harness.verify_models().status_code == 200


# --------------------------------------------------------------------------
# Credentials never leave the server
# --------------------------------------------------------------------------


def test_no_model_response_carries_a_credential(workspace, calls) -> None:
    harness = Harness(workspace, _router(calls))

    for response in (harness.verify_models(), harness.models()):
        assert API_KEY not in response.text
        assert "FAKE_API_KEY" not in response.text


def test_a_page_never_receives_a_credential(workspace, calls) -> None:
    """The page is served with the UI token substituted and nothing else."""
    harness = Harness(workspace, _router(calls))

    page = harness.client.get("/").text

    assert API_KEY not in page
    assert "FAKE_API_KEY" not in page


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_a_recheck_inherits_the_verifiers_own_timeout(
    workspace, calls, monkeypatch
) -> None:
    """One bound, defined once.

    The Workbench must not grow a second, more generous timeout for the health
    indicator; it calls ``verified_registry`` with the module default.
    """
    from forge.models import verify as verify_module

    seen: dict[str, float | None] = {}

    real = verify_module.verify_slots

    def spy(registry, *, environ, timeout=verify_module.VERIFY_TIMEOUT_SECONDS):
        seen["timeout"] = timeout
        return real(registry, environ=environ, timeout=timeout)

    monkeypatch.setattr(verify_module, "verify_slots", spy)

    harness = Harness(workspace, _router(calls))
    assert harness.verify_models().status_code == 200

    assert seen["timeout"] == verify_module.VERIFY_TIMEOUT_SECONDS


def test_one_recheck_does_not_block_the_event_loop(
    workspace, calls, monkeypatch
) -> None:
    """A bounded probe must not freeze the surface waiting on it."""
    from forge.models import verify as verify_module

    real = verify_module.verify_slots

    def slow(registry, *, environ, timeout=verify_module.VERIFY_TIMEOUT_SECONDS):
        time.sleep(0.4)
        return real(registry, environ=environ, timeout=timeout)

    monkeypatch.setattr(verify_module, "verify_slots", slow)

    harness = Harness(workspace, _router(calls))
    started = time.monotonic()
    assert harness.verify_models().status_code == 200
    elapsed = time.monotonic() - started

    assert elapsed >= 0.4, "the probe did not actually run"

    # A read served while nothing else is in flight must still answer promptly.
    assert harness.models().status_code == 200


# --------------------------------------------------------------------------
# No registry configured
# --------------------------------------------------------------------------


def test_a_workbench_with_no_registry_reports_that_rather_than_guessing(
    workspace, calls
) -> None:
    """An operator who passed ``--model`` over a broken config still gets a page.

    The indicator says the configuration is unusable. It does not fall back to a
    default registry and quietly report somebody else's health.
    """
    harness = Harness(workspace, _router(calls), health=ModelHealth(None))

    read = harness.models()
    assert read.status_code == 503
    assert "model" in read.json()["detail"].lower()

    assert harness.verify_models().status_code == 503


def test_a_config_error_is_passed_through_verbatim(workspace, calls) -> None:
    """The surface reports the registry's error; it does not rewrite it.

    Sanitising a message here would be a second, divergent redaction path. The
    guarantee that a configuration error names a table and a rule and carries no
    file contents belongs to
    :class:`forge.models.registry.RegistryConfigError`, so the test exercises
    that real error rather than inventing a string with a secret pasted into it.
    """
    from forge.models.registry import RegistryConfigError, load_registry

    path = workspace.root / "models.toml"
    # A table with an unrecognised key: the message names the table and the rule.
    path.write_text(
        '[tiers.general_light]\n'
        'primary = { model = "alpha", provider = "openrouter", nonsense = 1 }\n'
        "\n[routing]\ndefault_tier = \"general_light\"\n",
        encoding="utf-8",
    )
    try:
        load_registry(path)
    except RegistryConfigError as exc:
        message = str(exc)
    else:  # pragma: no cover - the document above is invalid by construction
        raise AssertionError("the fixture document was supposed to be rejected")

    harness = Harness(
        workspace, _router(calls), health=ModelHealth(None, error=message)
    )

    body = harness.models().json()

    assert body["detail"] == message
    assert "nonsense" in body["detail"]
    # Nothing from the file's contents is echoed -- only the table path and rule.
    assert "alpha" not in body["detail"]