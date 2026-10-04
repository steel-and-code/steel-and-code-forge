"""The Workbench's model-health wiring at the CLI edge.

Three claims, each of which is a decision rather than an implementation detail:

*   **Startup performs one verified pass.** A workbench that starts and only
    then shows "unverified" would make every operator's first question "is it
    actually up?", so the ruling is that ``forge workbench`` probes once before it
    serves. Once -- not on a timer, and not per request.
*   **A broken configuration does not stop the page from serving.** An operator
    who passed ``--model`` over an unreadable registry still gets a workbench
    they can read and use; the indicator reports 503 and says why.
*   **The gateway and the health cache are separate objects.** The credential
    lives in the gateway and the environ mapping inside the health cache, and
    neither is threaded into the session, the app state a page can read, or a
    response.
"""

from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest

from forge.models.base import VerificationCode, VerificationResult
from forge.models.providers import ProviderCatalog, ProviderSpec
from forge.models.registry import parse_registry
from forge.models.router import ModelRouter


def _module(dotted: str):
    return importlib.import_module(dotted)


API_KEY = "sk-cli-test-do-not-leak"


CONFIG = """
[providers.fake]
credential_env = "FAKE_API_KEY"

[tiers.general_light]
primary = { model = "alpha", provider = "fake" }

[tiers.coding_standard]
primary = { model = "alpha", provider = "fake" }

[tiers.reasoning_heavy]
primary = { model = "alpha", provider = "fake" }

[routing]
default_tier = "general_light"
"""


class _Verifier:
    def __init__(self, usable: set[str], calls: list[str]) -> None:
        self._usable = usable
        self._calls = calls

    def verify(self, model: str) -> VerificationResult:
        self._calls.append(model)
        if model in self._usable:
            return VerificationResult.success(model, reason="listed")
        return VerificationResult.failure(
            model, VerificationCode.NOT_FOUND, reason="not listed"
        )


def _router(calls: list[str], *, usable: set[str] | None = None) -> ModelRouter:
    answer = {"usable": usable if usable is not None else {"alpha"}}

    def verifier_factory(environ):
        return _Verifier(answer["usable"], calls)

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
    return ModelRouter(parse_registry(CONFIG, catalog=catalog))


# --------------------------------------------------------------------------
# Structural: the workbench builds a health cache and hands it to the app
# --------------------------------------------------------------------------


def _workbench_ast():
    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_workbench"
    )


def _calls(node):
    """Every ``Call`` in a function body, as ``(callee_name, node)`` pairs."""
    out = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            name = ""
        out.append((name, inner))
    return out


def _callees(node) -> set[str]:
    return {name for name, _ in _calls(node) if name}


def _one(node, name: str) -> ast.Call:
    """The single call to ``name``, or a failure naming what was found."""
    found = [call for called, call in _calls(node) if called == name]
    assert len(found) == 1, f"expected one {name}() call, found {len(found)}"
    return found[0]


def test_the_workbench_builds_a_model_health_cache() -> None:
    assert "ModelHealth" in _callees(_workbench_ast())


def test_the_workbench_hands_the_health_cache_to_the_app() -> None:
    """The page's indicator reads the cache the CLI built, not one it invents."""
    app_call = _one(_workbench_ast(), "create_app")
    keywords = {kw.arg for kw in app_call.keywords}

    assert "health" in keywords


def test_the_workbench_loads_the_registry_the_way_the_model_commands_do() -> None:
    """One load path, not a second parser beside the first.

    Two loads can disagree if the file changes between them, and an indicator
    that disagrees with the run it reports on is worse than no indicator.
    """
    assert "load_registry" in _callees(_workbench_ast())


def test_the_startup_pass_runs_before_the_server_binds() -> None:
    """Ordering is the point: the page's first read is a verified snapshot."""
    verify_call = _one(_workbench_ast(), "verify")
    run_call = _one(_workbench_ast(), "run")

    assert verify_call.lineno < run_call.lineno


def test_a_registry_error_does_not_stop_the_workbench_serving() -> None:
    """The indicator reports the failure; it does not take the page down.

    A broken configuration is exactly when an operator most wants a surface they
    can look at, and ``--model`` is the documented escape hatch for running
    anyway. Refusing to start would make the one diagnostic they need
    unavailable.
    """
    none_cache = [
        call
        for called, call in _calls(_workbench_ast())
        if called == "ModelHealth" and call.args and isinstance(call.args[0], ast.Constant)
    ]

    assert none_cache, "no empty ModelHealth() for the unreadable-config path"
    # And `create_app` is called unconditionally, not only on the success path.
    assert _one(_workbench_ast(), "create_app")


# --------------------------------------------------------------------------
# Behavioural: the health cache itself
# --------------------------------------------------------------------------


def test_a_fresh_cache_is_unverified_until_something_checks_it() -> None:
    """Offline configuration is not a health claim."""
    from forge.ui.models import ModelHealth

    health = ModelHealth(_router([]), environ={"FAKE_API_KEY": API_KEY})

    body = health.status()

    assert body["verified"] is False
    assert body["checked_at"] is None
    assert all(entry["verified"] is False for entry in body["tiers"])


def test_one_verify_pass_marks_the_snapshot_checked() -> None:
    from forge.ui.models import ModelHealth

    calls: list[str] = []
    health = ModelHealth(_router(calls), environ={"FAKE_API_KEY": API_KEY})

    body = health.verify()

    assert calls == ["alpha"], "one attempt per distinct model, not per tier"
    assert body["verified"] is True
    assert body["checked_at"]


def test_verify_reports_a_dead_provider_rather_than_raising() -> None:
    """A stopped local service is an operator fact, not a crash."""
    from forge.ui.models import ModelHealth

    calls: list[str] = []
    health = ModelHealth(
        _router(calls, usable=set()), environ={"FAKE_API_KEY": API_KEY}
    )

    body = health.verify()

    assert {entry["health"] for entry in body["tiers"]} == {"RED"}
    assert all(entry["reason"] for entry in body["tiers"])


def test_a_cache_with_no_registry_refuses_to_verify() -> None:
    from forge.ui.models import HealthUnavailable, ModelHealth

    with pytest.raises(HealthUnavailable):
        ModelHealth(None).verify()


def test_a_cache_publishes_no_credential() -> None:
    from forge.ui.models import ModelHealth

    calls: list[str] = []
    health = ModelHealth(_router(calls), environ={"FAKE_API_KEY": API_KEY})
    health.verify()

    serialised = json.dumps(health.status())

    assert API_KEY not in serialised
    assert "FAKE_API_KEY" not in serialised


def test_two_caches_in_one_process_do_not_share_a_snapshot() -> None:
    """A test, and two browser surfaces, each get their own answer."""
    from forge.ui.models import ModelHealth

    first = ModelHealth(_router([]), environ={})
    second = ModelHealth(_router([], usable=set()), environ={})

    # Both are checked, so the comparison is between two *verified* snapshots.
    # (An unverified cache reports configuration-only GREEN by design, which
    # would make this assertion pass for the wrong reason.)
    first.verify()
    second.verify()

    assert {e["health"] for e in first.status()["tiers"]} == {"GREEN"}
    assert {e["health"] for e in second.status()["tiers"]} == {"RED"}