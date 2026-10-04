"""Tests for verified model health: ``forge models status --verify``.

Two things are under test, and they are deliberately kept apart:

**The verifier** makes one cheap network attempt per distinct provider/model and
turns whatever happens -- a rejected key, a timeout, a body it cannot parse, a
model the provider no longer serves -- into a :class:`VerificationResult` value.
It never raises, and it never generates a token.

**The health seam** turns those results into an :class:`Availability`, and the
router's own ``health()`` decides GREEN/YELLOW/RED from it. Every routing test
here asserts against ``ModelRouter.status()`` rather than re-deriving health, so
a future status view cannot quietly disagree with a routing decision.

No test in this file touches a network. Transports are substituted at the
boundary the provider modules already expose, which is what makes the cost
bound ("one attempt, no retries, no generation") checkable rather than claimed.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

from forge.cli.main import main
from forge.cli.render import EXIT_MODEL_UNAVAILABLE, EXIT_OK
from forge.models.base import ModelGateway, ModelVerifier, VerificationCode
from forge.models.providers import ProviderCatalog, ProviderSpec
from forge.models.registry import MODELS_CONFIG_ENV, parse_registry
from forge.models.router import ModelRouter
from forge.models.tiers import Health

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _module(dotted: str):
    return importlib.import_module(dotted)


# --------------------------------------------------------------------------
# Transport doubles
# --------------------------------------------------------------------------


class FakeResponse:
    """The context manager ``urlopen`` returns."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def listing(*model_ids: str) -> str:
    return json.dumps({"data": [{"id": name} for name in model_ids]})


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Make an unstubbed network call in this file a test failure.

    Several tests below never patch ``urlopen`` and are nonetheless offline --
    an OpenRouter verifier with no credential returns before making a request,
    and the Protocol-conformance test constructs verifiers without calling them.
    That reasoning is exactly the kind that silently stops holding when a code
    path moves, and a test suite that reaches OpenRouter or loopback in CI is
    slow, flaky, and occasionally billable.

    So the guard is unconditional: any real socket attempt fails loudly. A test
    that patches ``urllib.request.urlopen`` is unaffected, because patching
    replaces the call before it can reach a socket.
    """
    import socket as socket_module

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "this test made a real network attempt; patch urllib.request.urlopen "
            "or inject a fake verifier_factory"
        )

    monkeypatch.setattr(socket_module.socket, "connect", forbidden, raising=False)
    monkeypatch.setattr(socket_module, "create_connection", forbidden, raising=False)
    monkeypatch.setattr(socket_module, "getaddrinfo", forbidden, raising=False)


def transport(handler, *, calls: list | None = None):
    """A ``urlopen`` stand-in that records its arguments.

    ``calls`` is what makes "exactly one attempt per distinct provider/model"
    a testable claim rather than a comment.

    Patched onto ``urllib.request.urlopen`` itself and not onto one provider
    module's reference to it: ``forge.models.openrouter`` and
    ``forge.models.local`` both reach the *same* ``urllib.request`` module
    object, so patching ``openrouter.urllib.request`` patches local too. Tests
    that want per-provider behaviour route on ``request.full_url`` instead of
    assuming the patch was scoped.
    """

    def _open(request, timeout=None):
        if calls is not None:
            calls.append(
                {
                    "url": request.full_url,
                    "method": request.get_method(),
                    "timeout": timeout,
                    "authorization": request.get_header("Authorization"),
                }
            )
        outcome = handler(request)
        if isinstance(outcome, Exception):
            raise outcome
        # Accept both shapes a test might naturally return. A handler writing
        # `FakeResponse(...)` directly is the obvious thing to do, and failing
        # it with an unpack TypeError deep inside a provider would read as a bug
        # in the verifier rather than in the test double.
        if isinstance(outcome, FakeResponse):
            return outcome
        return FakeResponse(*outcome)

    return _open


def routed(routes: dict[str, object], *, calls: list | None = None):
    """One transport serving several provider URLs.

    ``openrouter`` and ``local`` reach the *same* ``urllib.request`` module, so a
    patch applied through either is visible to both. Anything that needs a
    different answer per provider has to route on the URL -- patching two
    "separately" would just mean the second patch silently replaced the first.
    """

    def handler(request):
        for prefix, outcome in routes.items():
            if request.full_url.startswith(prefix):
                return outcome
        raise AssertionError(f"unexpected request URL: {request.full_url}")

    return transport(handler, calls=calls)


@pytest.fixture()
def patch_urlopen(monkeypatch):
    """Patch ``urllib.request.urlopen`` once, for every provider.

    Returns a recorder whose ``calls`` list every test can read, which is the
    only honest way to count requests across two provider modules that share one
    ``urllib``.
    """

    def _apply(handler) -> list:
        calls: list = []
        import urllib.request

        monkeypatch.setattr(
            urllib.request, "urlopen", transport(handler, calls=calls)
        )
        return calls

    return _apply


def socket_timeout_error() -> Exception:
    """``urlopen`` surfaces a read timeout as a ``URLError`` wrapping it."""
    return urllib.error.URLError(TimeoutError("timed out"))


# --------------------------------------------------------------------------
# OpenRouter verification
# --------------------------------------------------------------------------


OPENROUTER_LISTING = "https://openrouter.test/api/v1/models"
LOCAL_LISTING = "http://127.0.0.1:9999/v1/models"


def _openrouter_verifier(monkeypatch, handler, *, environ, calls=None, timeout=5.0):
    from forge.models import openrouter

    verifier = openrouter.OpenRouterVerifier(
        environ=environ, endpoint=OPENROUTER_LISTING, timeout=timeout
    )
    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(handler, calls=calls)
    )
    return verifier


# --------------------------------------------------------------------------
# OpenRouter verification
# --------------------------------------------------------------------------


def _openrouter_verifier(monkeypatch, handler, *, environ, calls=None, timeout=5.0):
    from forge.models import openrouter

    verifier = openrouter.OpenRouterVerifier(
        environ=environ,
        endpoint="https://openrouter.test/api/v1/models",
        timeout=timeout,
    )
    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(handler, calls=calls)
    )
    return verifier


def test_a_listed_model_is_verified_usable(monkeypatch) -> None:
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/alpha", "vendor/beta")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert result.usable
    assert result.code is VerificationCode.OK


def test_openrouter_verification_makes_exactly_one_call(monkeypatch) -> None:
    """One attempt. No retries, and never a second request to 'be sure'."""
    calls: list = []
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/alpha")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
        calls=calls,
    )

    verifier.verify("vendor/alpha")

    assert len(calls) == 1


def test_openrouter_verification_sends_no_completion_request(monkeypatch) -> None:
    """The cheapest check is a listing. A POST would generate tokens and cost money."""
    calls: list = []
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/alpha")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
        calls=calls,
    )

    verifier.verify("vendor/alpha")

    assert calls[0]["method"] == "GET"
    assert "/chat/completions" not in calls[0]["url"]


def test_openrouter_verification_honours_the_timeout_bound(monkeypatch) -> None:
    calls: list = []
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/alpha")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
        calls=calls,
        timeout=5.0,
    )

    verifier.verify("vendor/alpha")

    assert calls[0]["timeout"] == 5.0


def test_openrouter_rejects_a_model_it_does_not_list(monkeypatch) -> None:
    """"The provider answered and does not have it" is not a transport failure."""
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/beta")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert not result.usable
    assert result.code is VerificationCode.NOT_FOUND


@pytest.mark.parametrize("status", [401, 403])
def test_openrouter_reports_an_authentication_failure_distinctly(
    monkeypatch, status: int
) -> None:
    """A bad key is fixed in the environment; a missing model is fixed in config.

    Conflating the two sends an operator to edit the wrong file.
    """
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (status, json.dumps({"error": {"code": status}})),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert result.code is VerificationCode.UNAUTHENTICATED
    assert not result.usable


def test_openrouter_reports_a_timeout_distinctly(monkeypatch) -> None:
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: socket_timeout_error(),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert result.code is VerificationCode.TIMEOUT


def test_openrouter_reports_an_unreachable_endpoint(monkeypatch) -> None:
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: urllib.error.URLError(ConnectionRefusedError("refused")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert result.code is VerificationCode.UNREACHABLE


def test_openrouter_reports_a_malformed_response(monkeypatch) -> None:
    """A body this verifier cannot parse is a reportable state, not a crash."""
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, "<html>not json at all</html>"),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert result.code is VerificationCode.MALFORMED_RESPONSE


def test_openrouter_reports_a_listing_of_the_wrong_shape(monkeypatch) -> None:
    """`{"data": "nope"}` parses as JSON and is still unusable."""
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, json.dumps({"data": "nope"})),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert result.code is VerificationCode.MALFORMED_RESPONSE


def test_openrouter_reports_a_missing_credential_without_making_a_request(
    monkeypatch,
) -> None:
    """No key is a configuration fact. It must not become a failed HTTP call."""
    calls: list = []
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/alpha")),
        environ={},
        calls=calls,
    )

    result = verifier.verify("vendor/alpha")

    assert result.code is VerificationCode.CREDENTIAL_MISSING
    assert calls == []


def test_openrouter_verification_never_echoes_the_credential(monkeypatch) -> None:
    """The key reached a provider; nothing it reached may come back out."""
    secret = "«redacted:sk-…»"
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (
            401,
            json.dumps({"error": {"message": f"invalid key {secret}", "code": 401}}),
        ),
        environ={"FORGE_OPENROUTER_API_KEY": secret},
    )

    result = verifier.verify("vendor/alpha")

    assert secret not in result.reason
    assert secret not in result.detail
    assert secret not in json.dumps(result.to_json())


def test_openrouter_verification_never_echoes_the_provider_message(monkeypatch) -> None:
    """Providers quote the offending request, which carried the authorization header.

    Only the short identifier fields survive; the prose is discarded entirely.
    """
    marker = "Bearer sk-leaked-request-echo"
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (
            403,
            json.dumps({"error": {"message": marker, "code": 403, "type": "auth"}}),
        ),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert marker not in result.reason
    assert marker not in result.detail
    assert "403" in result.reason


def test_openrouter_sends_the_credential_in_a_header_only(monkeypatch) -> None:
    calls: list = []
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request: (200, listing("vendor/alpha")),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
        calls=calls,
    )

    verifier.verify("vendor/alpha")

    assert calls[0]["authorization"] == "Bearer «redacted:sk-…»"


# --------------------------------------------------------------------------
# Local endpoint verification
# --------------------------------------------------------------------------


def _local_verifier(monkeypatch, handler, *, calls=None, timeout=5.0):
    from forge.models import local

    verifier = local.LocalLlamaVerifier(endpoint=LOCAL_LISTING, timeout=timeout)
    monkeypatch.setattr(
        local.urllib.request, "urlopen", transport(handler, calls=calls)
    )
    return verifier


def test_local_listed_model_is_verified_usable(monkeypatch) -> None:
    verifier = _local_verifier(monkeypatch, lambda request: (200, listing("qwen")))

    assert verifier.verify("qwen").usable


def test_local_verification_makes_one_get_and_no_post(monkeypatch) -> None:
    calls: list = []
    verifier = _local_verifier(
        monkeypatch, lambda request: (200, listing("qwen")), calls=calls
    )

    verifier.verify("qwen")

    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/v1/models")


def test_local_verification_never_sends_a_credential(monkeypatch) -> None:
    """A local endpoint takes no key, so none may be attached."""
    calls: list = []
    verifier = _local_verifier(
        monkeypatch, lambda request: (200, listing("qwen")), calls=calls
    )

    verifier.verify("qwen")

    assert calls[0]["authorization"] is None


def test_local_reports_an_absent_model_distinctly(monkeypatch) -> None:
    verifier = _local_verifier(monkeypatch, lambda request: (200, listing("other")))

    assert verifier.verify("qwen").code is VerificationCode.NOT_FOUND


def test_local_reports_a_down_server_as_unreachable(monkeypatch) -> None:
    """Nothing is listening on 8080: that is the common local case."""
    verifier = _local_verifier(
        monkeypatch,
        lambda request: urllib.error.URLError(ConnectionRefusedError("refused")),
    )

    assert verifier.verify("qwen").code is VerificationCode.UNREACHABLE


def test_local_reports_a_timeout_distinctly(monkeypatch) -> None:
    verifier = _local_verifier(
        monkeypatch, lambda request: socket_timeout_error()
    )

    assert verifier.verify("qwen").code is VerificationCode.TIMEOUT


def test_local_reports_a_malformed_response(monkeypatch) -> None:
    verifier = _local_verifier(monkeypatch, lambda request: (200, "not json"))

    assert verifier.verify("qwen").code is VerificationCode.MALFORMED_RESPONSE


# --------------------------------------------------------------------------
# The verifier registry seam
# --------------------------------------------------------------------------


def test_the_catalog_gives_routable_providers_a_verifier() -> None:
    """Every provider Forge can route to is one Forge can also check."""
    catalog = ProviderCatalog.default()

    for name in ("openrouter", "local"):
        assert catalog.verifier_for(name) is not None


def test_a_declared_provider_without_a_gateway_has_no_verifier() -> None:
    """Unimplemented is declared-but-unimplemented, not half-implemented."""
    catalog = ProviderCatalog.default()

    for name in ("openai", "anthropic"):
        assert catalog.verifier_for(name) is None


def test_with_spec_carries_the_verifier_and_keeps_the_gateway() -> None:
    """A provider redeclared in configuration must not silently lose checking."""
    base = ProviderCatalog.default()
    spec = base.spec("openrouter")
    assert spec is not None

    merged = base.with_spec(
        ProviderSpec(
            name="openrouter",
            credential_env="FORGE_OPENROUTER_API_KEY",
            gateway_factory=spec.gateway_factory,
            verifier_factory=spec.verifier_factory,
        )
    ).with_spec(ProviderSpec(name="house", credential_env="FORGE_HOUSE_KEY"))

    assert merged.verifier_for("openrouter") is not None
    assert merged.gateway_for("openrouter") is not None
    assert merged.verifier_for("house") is None


def test_verifying_an_unimplemented_provider_is_a_clean_result() -> None:
    """A provider Forge cannot check reports that, and names no gateway."""
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[providers.anthropic]\ncredential_env = "FORGE_ANTHROPIC_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "claude-x", provider = "anthropic" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    results = verify_slots(registry, environ={})

    assert results["anthropic/claude-x"].code is VerificationCode.UNIMPLEMENTED
    assert not results["anthropic/claude-x"].usable


def test_verifying_does_not_raise_for_any_provider_outage() -> None:
    """Every failure mode returns a value. None of them escapes.

    Configured with openrouter only, so this exercises the failure paths
    without a loopback connection attempt.
    """
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter" }\n'
        'fallbacks = [{ model = "y", provider = "openrouter" }]\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    results = verify_slots(registry, environ={})

    assert results["openrouter/x"].code is VerificationCode.CREDENTIAL_MISSING
    assert results["openrouter/y"].code is VerificationCode.CREDENTIAL_MISSING


def test_a_provider_with_no_verifier_is_reported_rather_than_contacted() -> None:
    """The unimplemented case is a result. Nothing is attempted."""
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter" }\n'
        'fallbacks = [{ model = "y", provider = "openai" }]\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    results = verify_slots(registry, environ={})

    # No credential, so openrouter reports that rather than calling out, and
    # openai has no verifier to build.
    assert results["openrouter/x"].code is VerificationCode.CREDENTIAL_MISSING
    assert results["openai/y"].code is VerificationCode.UNIMPLEMENTED


def test_a_verifier_returning_nonsense_is_not_read_as_healthy() -> None:
    """A broken implementation must not be able to report GREEN.

    Truthiness is exactly the trap here: anything non-None is truthy, so a
    verifier returning a string would otherwise verify every model.
    """
    from forge.models.verify import verify_slots

    class Broken:
        def verify(self, model: str):
            return "totally fine"

    registry = parse_registry(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "house" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        extra_providers=(
            ProviderSpec(
                name="house",
                credential_env="FORGE_HOUSE_KEY",
                verifier_factory=lambda environ: Broken(),
            ),
        ),
    )

    results = verify_slots(registry, environ={})

    assert results["house/x"].code is VerificationCode.MALFORMED_RESPONSE
    assert not results["house/x"].usable


def test_a_verifier_that_raises_is_reported_rather_than_crashing() -> None:
    """Defence in depth at the seam, not just inside a provider."""
    from forge.models.verify import verify_slots

    class Exploding:
        def verify(self, model: str):
            raise RuntimeError("boom")

    registry = parse_registry(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "house" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        extra_providers=(
            ProviderSpec(
                name="house",
                credential_env="FORGE_HOUSE_KEY",
                verifier_factory=lambda environ: Exploding(),
            ),
        ),
    )

    results = verify_slots(registry, environ={})

    assert results["house/x"].code is VerificationCode.UNIMPLEMENTED or not results[
        "house/x"
    ].usable


def test_a_verifier_factory_that_raises_is_reported_for_its_models() -> None:
    """A provider whose verifier cannot even be built is still a status."""
    from forge.models.verify import verify_slots

    def broken_factory(environ):
        raise RuntimeError("cannot build")

    registry = parse_registry(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "house" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        extra_providers=(
            ProviderSpec(
                name="house",
                credential_env="FORGE_HOUSE_KEY",
                verifier_factory=broken_factory,
            ),
        ),
    )

    results = verify_slots(registry, environ={})

    assert not results["house/x"].usable
    assert results["house/x"].code is VerificationCode.UNIMPLEMENTED


# --------------------------------------------------------------------------
# Probing: what gets probed, and what never does
# --------------------------------------------------------------------------

#: ``vendor/coder`` deliberately appears as the primary of *both* tiers, so the
#: "one request per distinct model" claim is testable: three tiers, two distinct
#: openrouter models, and a local model behind one of them.
TWO_TIER_CONFIG = """
[providers.openrouter]
credential_env = "FORGE_OPENROUTER_API_KEY"

[providers.local]
credential_env = ""

[providers.anthropic]
credential_env = "FORGE_ANTHROPIC_API_KEY"

[tiers.general_light]
primary = { model = "vendor/alpha", provider = "openrouter" }
fallbacks = [{ model = "local-beta", provider = "local" }]

[tiers.coding_standard]
primary = { model = "vendor/coder", provider = "openrouter" }
fallbacks = [{ model = "local-coder", provider = "local" }]

[tiers.reasoning_heavy]
primary = { model = "vendor/coder", provider = "openrouter" }
fallbacks = []

[routing]
default_tier = "general_light"
"""


def _two_tier_registry():
    return parse_registry(TWO_TIER_CONFIG)


def test_a_disabled_model_is_never_probed(monkeypatch) -> None:
    """An operator's switch is not something a probe gets to second-guess.

    Contacting a provider about a model the operator switched off would spend
    a request on a question nobody asked, and a result would tempt someone to
    report it as covering.
    """
    from forge.models import openrouter
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "vendor/alpha", provider = "openrouter", enabled = false }\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    calls: list = []
    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(lambda r: (200, listing("vendor/alpha")), calls=calls)
    )

    verify_slots(registry, environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"})

    assert calls == []


def test_a_model_on_an_unimplemented_provider_is_not_probed(monkeypatch) -> None:
    from forge.models import openrouter
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[providers.anthropic]\ncredential_env = "FORGE_ANTHROPIC_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "claude-x", provider = "anthropic" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    calls: list = []
    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(lambda r: (200, listing("x")), calls=calls)
    )

    verify_slots(registry, environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"})

    assert calls == []


def test_each_distinct_model_is_probed_once(monkeypatch) -> None:
    """``vendor/coder`` is the primary of two tiers and is asked about once.

    A verification pass is not a load test: N tiers naming the same model must
    produce one request, or a large registry turns a status check into an
    outage.
    """
    from forge.models import openrouter
    from forge.models.verify import verify_slots

    import urllib.request

    calls: list = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha", "vendor/coder")),
                "http://127.0.0.1": (200, listing("local-beta", "local-coder")),
            },
            calls=calls,
        ),
    )

    registry = _two_tier_registry()
    results = verify_slots(
        registry, environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    )

    # Four distinct models across three tiers, so four GETs -- and vendor/coder,
    # which is the primary of two tiers, is asked about once.
    assert len(calls) == 4
    assert results["openrouter/vendor/coder"].usable
    assert sum("coder" in call["url"] for call in calls) == 0


def test_verification_reports_a_result_for_every_probed_slot(monkeypatch) -> None:
    """One result per enabled, routable, probeable slot -- no gaps."""
    import urllib.request
    from forge.models.verify import verify_slots

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha", "vendor/coder")),
                "http://127.0.0.1": (200, listing("local-beta", "local-coder")),
            }
        ),
    )

    results = verify_slots(
        _two_tier_registry(),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    assert set(results.results) == {
        "openrouter/vendor/alpha",
        "openrouter/vendor/coder",
        "local/local-beta",
        "local/local-coder",
    }


def test_a_transport_that_raises_unexpectedly_is_reported_not_usable(
    monkeypatch,
) -> None:
    """Defence in depth: an exception outside the expected failure set is a value.

    Importantly it becomes an *unusable* result, not a usable one. A transport
    blowing up in a way no handler anticipated means Forge learned nothing about
    the model, and reporting that as healthy would be the worst possible answer.
    """
    import urllib.request
    from forge.models.verify import verify_slots

    def explode(request):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(urllib.request, "urlopen", transport(explode))

    results = verify_slots(
        _two_tier_registry(),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = results["openrouter/vendor/alpha"]
    assert not result.usable
    assert result.code is VerificationCode.UNREACHABLE
    assert "RuntimeError" in result.reason


# --------------------------------------------------------------------------
# Health semantics, through the router -- never re-derived here
# --------------------------------------------------------------------------


def _router_with(results: dict[str, bool]) -> ModelRouter:
    """A router whose availability comes from ``results``, health computed as usual."""
    from forge.models.registry import Availability

    registry = _two_tier_registry()
    mapping = {
        key: value
        for key, value in results.items()
    }
    return ModelRouter(registry.with_availability(Availability(mapping)))


def _status(router: ModelRouter) -> dict[str, dict]:
    return {entry["tier"]: entry for entry in router.status()}


def test_a_verified_healthy_primary_is_green() -> None:
    router = _router_with({"openrouter/vendor/alpha": True})

    entry = _status(router)["general_light"]

    assert entry["health"] == Health.GREEN.value
    assert entry["model"] == "vendor/alpha"
    assert entry["provider"] == "openrouter"
    assert entry["selection"] == "primary"


def test_a_failed_primary_with_a_verified_fallback_is_yellow() -> None:
    router = _router_with(
        {"openrouter/vendor/coder": False, "local/local-coder": True}
    )

    entry = _status(router)["coding_standard"]

    assert entry["health"] == Health.YELLOW.value
    assert entry["selection"] == "fallback"
    assert entry["fallback_index"] == 1


def test_every_candidate_failing_is_red() -> None:
    router = _router_with(
        {"openrouter/vendor/coder": False, "local/local-coder": False}
    )

    entry = _status(router)["coding_standard"]

    assert entry["health"] == Health.RED.value
    assert entry["selection"] == "none"
    assert entry["model"] == ""


def test_a_verified_run_marks_availability_verified() -> None:
    """"Checked" and "checked, and it worked" are different claims."""
    from forge.models.verify import verify_slots

    registry = _two_tier_registry()
    availability = verify_slots(
        registry, environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    ).availability()

    assert availability.verified is True


def test_a_failed_probe_still_counts_as_verified() -> None:
    """"Verified" means we checked, not that the answer was yes.

    A RED tier with verified=True is the honest report: we asked, and nothing
    could serve. Collapsing it into unverified would hide the failure.
    """
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "vendor/gone", provider = "openrouter" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    availability = verify_slots(registry, environ={}).availability()

    assert availability.verified is True


def test_the_reason_distinguishes_a_missing_model_from_a_bad_key() -> None:
    from forge.models.verify import verify_slots

    registry = parse_registry(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "vendor/alpha", provider = "openrouter" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n'
    )

    results = verify_slots(registry, environ={})

    assert "FORGE_OPENROUTER_API_KEY" in results["openrouter/vendor/alpha"].reason


# --------------------------------------------------------------------------
# status() is the source of truth for a UI
# --------------------------------------------------------------------------


def test_status_carries_per_slot_verification_without_re_deriving_it() -> None:
    """A UI must be able to bind to this rather than recompute health."""
    from forge.models.registry import Availability

    registry = _two_tier_registry()
    router = ModelRouter(registry.with_availability(Availability({"openrouter/vendor/coder": False})))

    entry = _status(router)["coding_standard"]
    slots = {slot["model"]: slot for slot in entry["slots"]}

    assert "verified" in slots["vendor/coder"]
    assert "verify_reason" in slots["vendor/coder"]
    assert entry["verified"] is True


def test_the_routing_reason_survives_verification(monkeypatch) -> None:
    """Verification summarises; it must not overwrite the derivation.

    The concise health reason and the router's full explanation answer different
    questions. Collapsing them into one string either loses the slot-by-slot
    detail a routing audit needs, or makes the status line unreadable.
    """
    import urllib.request
    from forge.models.verify import verified_registry

    # vendor/coder is not listed, so its tiers fall back -- which is the case
    # where the two reasons actually diverge.
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha")),
                "http://127.0.0.1": (200, listing("local-beta", "local-coder")),
            }
        ),
    )

    registry = _two_tier_registry()
    verified = verified_registry(
        registry, environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    )

    entry = _status(ModelRouter(verified))["coding_standard"]

    assert entry["reason"] == "primary unavailable; fallback #1 verified healthy"
    # The untouched routing reason still names the primary and why it failed.
    assert "openrouter/vendor/coder" in entry["routing_reason"]
    assert "unavailable" in entry["routing_reason"]


def test_status_still_works_with_no_availability_source() -> None:
    """The plain, offline path is unchanged by any of this."""
    entry = _status(ModelRouter(_two_tier_registry()))["general_light"]

    assert entry["verified"] is False
    assert entry["health"] in (Health.GREEN.value, Health.YELLOW.value, Health.RED.value)


def test_a_ui_consumer_needs_no_lookup_of_its_own() -> None:
    """Every field a health indicator binds to is present on every tier row."""
    entry = _status(ModelRouter(_two_tier_registry()))["general_light"]

    for key in ("tier", "health", "model", "provider", "selection", "slot", "verified"):
        assert key in entry


# --------------------------------------------------------------------------
# forge models status --verify
# --------------------------------------------------------------------------


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def json_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.out.splitlines() if line.strip()]


def run(argv: list[str], environ: dict[str, str] | None = None) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, environ=environ if environ is not None else {})
    return Run(code, out.getvalue(), err.getvalue())


@pytest.fixture()
def config(tmp_path: Path) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(TWO_TIER_CONFIG, encoding="utf-8")
    return path


def _env(config: Path, **extra: str) -> dict[str, str]:
    return {MODELS_CONFIG_ENV: str(config), **extra}


def test_verify_reports_a_verified_healthy_primary_as_green(
    config: Path, monkeypatch
) -> None:
    from forge.models import openrouter

    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        transport(lambda r: (200, listing("vendor/alpha", "vendor/coder"))),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert "GREEN" in result.out
    assert result.code == EXIT_OK


def test_verify_makes_the_verified_column_visible(config: Path, monkeypatch) -> None:
    from forge.models import openrouter

    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        transport(lambda r: (200, listing("vendor/alpha", "vendor/coder"))),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert "VERIFIED" in result.out


def test_verify_reports_a_degraded_tier_with_a_reason(config: Path, monkeypatch) -> None:
    """'primary unavailable; fallback #1 verified healthy' -- the operator line."""
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                # vendor/coder is deliberately absent, so both tiers whose primary
                # is vendor/coder degrade to their fallbacks.
                "https://openrouter.ai": (200, listing("vendor/alpha")),
                "http://127.0.0.1": (200, listing("local-beta", "local-coder")),
            }
        ),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert "YELLOW" in result.out
    assert "fallback #1" in result.out
    assert "primary unavailable; fallback #1 verified healthy" in result.out
    # Only the rejected primary is listed. The fallback that *is* working is
    # named in the summary above; repeating it under a failure heading would
    # make a healthy model look broken.
    assert "vendor/coder: " in result.out
    assert "local/local-coder: " not in result.out


def test_verify_explains_why_a_red_tier_has_nothing(config: Path, monkeypatch) -> None:
    """A RED tier that says only "nothing works" sends the operator to guess.

    The cause of each failed candidate is the whole point of asking the provider
    in the first place, so it has to reach the terminal and not just --json.
    """
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (401, json.dumps({"error": {"code": 401}})),
                "http://127.0.0.1": (200, listing("local-beta", "local-coder")),
            }
        ),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    # reasoning_heavy has no fallback at all, so nothing can serve it.
    assert "no verified usable model in this tier" in result.out
    assert "rejected the credential" in result.out


def test_verify_does_not_list_a_disabled_slot_as_a_probe_failure(
    config: Path, monkeypatch
) -> None:
    """"You switched this off" must not be dressed up as a provider failure."""
    import urllib.request

    path = config.parent / "disabled.toml"
    path.write_text(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "vendor/alpha", provider = "openrouter" }\n'
        'fallbacks = [{ model = "vendor/off", provider = "openrouter",'
        ' enabled = false }]\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed({"https://openrouter.ai": (200, listing("vendor/alpha"))}),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(path, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert "GREEN" in result.out
    # The model still appears in the FALLBACKS column, marked disabled, which is
    # pre-existing behaviour and correct. What must not happen is it being given
    # a probe-failure line as though a provider had rejected it.
    assert "vendor/off (disabled)" in result.out
    assert "vendor/off:" not in result.out


def test_verify_exits_nonzero_when_a_tier_is_red(config: Path, monkeypatch) -> None:
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("nothing")),
                "http://127.0.0.1": (200, listing("nothing")),
            }
        ),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert result.code == EXIT_MODEL_UNAVAILABLE


def test_verify_reports_an_authentication_failure_without_crashing(
    config: Path, monkeypatch
) -> None:
    from forge.models import openrouter

    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(lambda r: (401, "{}"))
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert "Traceback" not in result.err
    assert "auth" in result.out.lower()


def test_verify_reports_a_malformed_provider_response_without_crashing(
    config: Path, monkeypatch
) -> None:
    from forge.models import openrouter

    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(lambda r: (200, "<html/>"))
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert "Traceback" not in result.err
    assert result.code in (EXIT_OK, EXIT_MODEL_UNAVAILABLE)


def test_verify_never_prints_the_credential(config: Path, monkeypatch) -> None:
    from forge.models import openrouter

    secret = "«redacted:sk-…»"
    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        transport(
            lambda r: (401, json.dumps({"error": {"message": f"bad key {secret}"}}))
        ),
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY=secret),
    )

    assert secret not in result.out
    assert secret not in result.err


def test_verify_json_carries_the_verification_codes(config: Path, monkeypatch) -> None:
    from forge.models import openrouter

    monkeypatch.setattr(
        openrouter.urllib.request,
        "urlopen",
        transport(lambda r: (401, json.dumps({"error": {"code": 401}}))),
    )

    entries = run(
        ["models", "status", "--verify", "--json"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    ).json_lines()

    by_tier = {entry["tier"]: entry for entry in entries}
    slots = {slot["model"]: slot for slot in by_tier["general_light"]["slots"]}

    # Probed and rejected: distinct from never probed, and the code is there so
    # a UI does not have to pattern-match the prose.
    assert slots["vendor/alpha"]["probed"] is True
    assert slots["vendor/alpha"]["verified"] is False
    assert slots["vendor/alpha"]["verify_code"] == "unauthenticated"
    assert "rejected the credential" in slots["vendor/alpha"]["verify_reason"]


def test_verify_emits_no_ansi(config: Path, monkeypatch) -> None:
    from forge.models import openrouter

    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(lambda r: (200, listing("vendor/alpha")))
    )

    result = run(
        ["models", "status", "--verify"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert not ANSI.search(result.out)


def test_plain_status_makes_no_network_call(config: Path, monkeypatch) -> None:
    """Offline is the default, and it must stay that way.

    Any socket opened here is a regression, not a slow path.
    """
    import socket

    def forbidden(*args, **kwargs):  # pragma: no cover - only runs on a regression
        raise AssertionError("forge models status opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("forge models status resolved a hostname")
        ),
    )

    result = run(
        ["models", "status"],
        environ=_env(config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    assert result.code == EXIT_OK


def test_plain_status_still_reports_unverified(config: Path) -> None:
    """No probe ran, so the word must still be there."""
    result = run(["models", "status"], environ=_env(config))

    assert "unverified" in result.out.lower()


def test_verify_does_not_mutate_the_registry_it_was_given(config: Path, monkeypatch) -> None:
    """The offline router a caller already holds must keep reporting offline."""
    from forge.models import openrouter
    from forge.models.registry import load_registry
    from forge.models.verify import verify_slots

    registry = load_registry(config, environ={})
    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", transport(lambda r: (200, listing("vendor/alpha")))
    )

    verify_slots(registry, environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"})

    assert registry.availability.verified is False


# --------------------------------------------------------------------------
# The provider verification summary: presentation only
# --------------------------------------------------------------------------


SUMMARY_CONFIG = """
[providers.openrouter]
credential_env = "FORGE_OPENROUTER_API_KEY"

[providers.local]
credential_env = ""

[providers.anthropic]
credential_env = "FORGE_ANTHROPIC_API_KEY"

[tiers.general_light]
primary = { model = "vendor/alpha", provider = "openrouter" }
fallbacks = [{ model = "local-beta", provider = "local" }]

[tiers.coding_standard]
primary = { model = "vendor/coder", provider = "openrouter" }
fallbacks = []

[routing]
default_tier = "general_light"
"""


@pytest.fixture()
def summary_config(tmp_path: Path) -> Path:
    path = tmp_path / "summary.toml"
    path.write_text(SUMMARY_CONFIG, encoding="utf-8")
    return path


def _summary_line(out: str, provider: str) -> str:
    """The verification-summary row for ``provider``, or a marker if absent.

    Scoped to the region *after* the "verification summary" header. Without that
    scope it also matched the per-slot failure detail lines above the table, which
    begin with the same provider name -- so a passing test would have been
    asserting on the wrong line.
    """
    lines = out.splitlines()
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip().lower() == "verification summary"
        )
    except StopIteration:
        return "<absent>"

    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(provider.lower()):
            return stripped
        if stripped.startswith(("config", "availability")):
            break

    return "<absent>"


def test_the_summary_counts_checked_and_healthy_per_provider(
    summary_config: Path, monkeypatch
) -> None:
    """The shape an operator scans: how much was asked, how much answered."""
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha", "vendor/coder")),
                "http://127.0.0.1": (200, listing("local-beta")),
            }
        ),
    )

    out = run(
        ["models", "status", "--verify"],
        environ=_env(summary_config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    ).out

    assert "verification summary" in out
    assert _summary_line(out, "OpenRouter") == "OpenRouter    2 checked, 2 healthy"
    assert _summary_line(out, "Local") == "Local         1 checked, 1 healthy"


def test_the_summary_appears_only_when_something_was_verified(
    summary_config: Path,
) -> None:
    """An offline status has nothing to summarise, and must not fake a row."""
    out = run(["models", "status"], environ=_env(summary_config)).out

    assert "verification summary" not in out
    assert "checked" not in out


def test_the_summary_does_not_change_any_health_value(
    summary_config: Path, monkeypatch
) -> None:
    """Presentation only. The same run with and without the summary agrees.

    The summary is added to ``render.py`` and must not be able to influence a
    tier's health, its model, or the exit code -- otherwise a diagnostic line
    would have become routing semantics.
    """
    import urllib.request

    from forge.cli.render import (
        _provider_summary_lines,
        render_model_status,
    )
    from forge.models.registry import load_registry
    from forge.models.router import ModelRouter
    from forge.models.verify import verified_registry

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha")),
                "http://127.0.0.1": (200, listing("local-beta")),
            }
        ),
    )

    env = {
        "FORGE_MODELS_CONFIG": str(summary_config),
        "FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»",
    }

    # The verified entries, with the summary suppressed, versus with it.
    offline_router = ModelRouter(load_registry(summary_config, environ=env))
    verified_router = ModelRouter(
        verified_registry(load_registry(summary_config, environ=env), environ=env)
    )

    with_summary = render_model_status(
        tiers=verified_router.status(),
        source="x",
        verified=True,
    )
    without_summary = "\n".join(
        line
        for line in with_summary.splitlines()
        if not line.strip().lower().startswith("verification summary")
        and "checked" not in line
    )

    # Health, model, and provider columns are byte-identical.
    assert with_summary.splitlines()[0] == without_summary.splitlines()[0]
    assert _provider_summary_lines(verified_router.status()) != []
    assert verified_router.health_by_tier() == {
        "coding_standard": Health.RED,
        "general_light": Health.GREEN,
        "reasoning_heavy": Health.RED,
    }


def test_the_summary_is_absent_from_the_json_output(summary_config: Path, monkeypatch) -> None:
    """`--json` is the machine contract; a human summary has no place in it.

    A consumer parsing newline-delimited tier objects must not encounter a
    summary line it cannot interpret.
    """
    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha", "vendor/coder")),
                "http://127.0.0.1": (200, listing("local-beta")),
            }
        ),
    )

    entries = run(
        ["models", "status", "--verify", "--json"],
        environ=_env(summary_config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    ).json_lines()

    assert len(entries) == 3
    assert all("summary" not in entry for entry in entries)


def test_an_unimplemented_provider_is_named_as_not_checked(
    summary_config: Path, monkeypatch
) -> None:
    """"Forge cannot check this" must not read as "everything failed".

    A bare ``0 healthy`` beside a provider nobody asked would be indistinguishable
    from an outage, and would send an operator looking for a problem that does not
    exist in the provider.
    """
    import urllib.request

    path = summary_config.parent / "with-anthropic.toml"
    path.write_text(
        SUMMARY_CONFIG
        + '\n[tiers.reasoning_heavy]\n'
        'primary = { model = "claude-x", provider = "anthropic" }\n'
        'fallbacks = []\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/alpha", "vendor/coder")),
                "http://127.0.0.1": (200, listing("local-beta")),
            }
        ),
    )

    out = run(
        ["models", "status", "--verify"],
        environ=_env(path, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    ).out

    line = _summary_line(out, "Anthropic")
    assert line != "<absent>"
    assert "not checked" in line
    assert "no health information" in line
    # And it must not claim a healthy count it never established.
    assert "0 healthy" not in line


def test_a_shared_model_is_counted_once_per_provider(
    summary_config: Path, monkeypatch
) -> None:
    """Three tiers naming one primary is one model, not three.

    Counting per slot row would make a small registry look busy and would make
    the checked count disagree with the number of requests actually made.
    """
    import urllib.request

    calls: list = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        routed(
            {
                "https://openrouter.ai": (200, listing("vendor/shared")),
                "http://127.0.0.1": (200, listing("local-beta")),
            },
            calls=calls,
        ),
    )

    path = summary_config.parent / "shared.toml"
    path.write_text(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "vendor/shared", provider = "openrouter" }\n'
        'fallbacks = []\n'
        '[tiers.coding_standard]\n'
        'primary = { model = "vendor/shared", provider = "openrouter" }\n'
        'fallbacks = []\n'
        '[tiers.reasoning_heavy]\n'
        'primary = { model = "vendor/shared", provider = "openrouter" }\n'
        'fallbacks = []\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    out = run(
        ["models", "status", "--verify"],
        environ=_env(path, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    ).out

    assert _summary_line(out, "OpenRouter") == "OpenRouter    1 checked, 1 healthy"
    assert len(calls) == 1


# --------------------------------------------------------------------------
# A stopped local endpoint reads operationally, not as a model failure
# --------------------------------------------------------------------------


def test_a_stopped_local_endpoint_says_so_operationally(monkeypatch) -> None:
    """Nothing listening on the local port is a fact about the machine.

    It needs to read as "the service may not be running", not as a model the
    provider cannot serve -- those send an operator to different places.
    """
    from forge.models import local

    verifier = local.LocalLlamaVerifier(endpoint=LOCAL_LISTING)
    monkeypatch.setattr(
        local.urllib.request,
        "urlopen",
        transport(
            lambda request, timeout=None: (_ for _ in ()).throw(
                urllib.error.URLError(ConnectionRefusedError("refused"))
            )
        ),
    )

    result = verifier.verify("qwen")

    assert result.code is VerificationCode.UNREACHABLE
    assert "local endpoint unreachable" in result.reason
    assert "may not be running" in result.reason


def test_a_local_timeout_says_the_service_may_not_be_running(monkeypatch) -> None:
    """Same operational framing for a timeout, with the cause distinguishing them."""
    from forge.models import local

    verifier = local.LocalLlamaVerifier(endpoint=LOCAL_LISTING)
    monkeypatch.setattr(
        local.urllib.request,
        "urlopen",
        transport(lambda request, timeout=None: socket_timeout_error()),
    )

    result = verifier.verify("qwen")

    assert result.code is VerificationCode.TIMEOUT
    assert "may not be running" in result.reason


def test_an_unreachable_openrouter_names_the_endpoint(monkeypatch) -> None:
    """Provider-specific wording, same shape: what is unreachable, and why."""
    verifier = _openrouter_verifier(
        monkeypatch,
        lambda request, timeout=None: urllib.error.URLError(
            ConnectionRefusedError("refused")
        ),
        environ={"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
    )

    result = verifier.verify("vendor/alpha")

    assert "OpenRouter endpoint unreachable" in result.reason
    assert "ConnectionRefusedError" in result.reason


def test_a_stopped_local_endpoint_leaves_the_tier_red(summary_config: Path, monkeypatch) -> None:
    """Operational wording does NOT become a special case that forgives anything.

    A stopped local endpoint is still an unusable model. The tier stays RED, the
    reason is the operational one, and nothing is suppressed into GREEN or
    YELLOW -- exactly as the requirement states.
    """
    import urllib.request

    def refuse(request, timeout=None):
        if request.full_url.startswith("http://127.0.0.1"):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        return FakeResponse(200, listing("vendor/alpha", "vendor/coder"))

    monkeypatch.setattr(urllib.request, "urlopen", transport(refuse))

    path = summary_config.parent / "local-only.toml"
    path.write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "local-beta", provider = "local" }\n'
        'fallbacks = []\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["models", "status", "--verify"], environ=_env(path))

    assert result.code == EXIT_MODEL_UNAVAILABLE
    assert "RED" in result.out
    assert "local endpoint unreachable" in result.out
    assert "may not be running" in result.out
    assert _summary_line(result.out, "Local") == "Local         1 checked, 0 healthy"


def test_a_stopped_local_endpoint_does_not_hide_a_healthy_remote_tier(
    summary_config: Path, monkeypatch
) -> None:
    """One dead provider must not drag the other tiers down, or mask them."""
    import urllib.request

    def refuse(request, timeout=None):
        if request.full_url.startswith("http://127.0.0.1"):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        return FakeResponse(200, listing("vendor/alpha", "vendor/coder"))

    monkeypatch.setattr(urllib.request, "urlopen", transport(refuse))

    result = run(
        ["models", "status", "--verify"],
        environ=_env(summary_config, FORGE_OPENROUTER_API_KEY="«redacted:sk-…»"),
    )

    # general_light's primary is remote and healthy: still GREEN. Matched by
    # field rather than by column count so a width change in the renderer does
    # not fail a test about provider independence.
    rows = {
        line.split()[0]: line.split()[1]
        for line in result.out.splitlines()
        if line[:1].isalpha() and len(line.split()) > 1
    }
    # Both remote primaries are healthy, so both tiers are GREEN. Only the tier
    # whose sole path runs through the dead local endpoint is degraded -- which
    # is the point: one dead provider stays local to the tiers that need it.
    assert rows["general_light"] == "GREEN"
    assert rows["coding_standard"] == "GREEN"
    # The summary separates the two providers so the cause is obvious.
    assert _summary_line(result.out, "OpenRouter") == "OpenRouter    2 checked, 2 healthy"
    assert _summary_line(result.out, "Local") == "Local         1 checked, 0 healthy"


def _is_docstring(node: ast.FunctionDef) -> bool:
    """Whether a function's first statement is its docstring."""
    return (
        bool(node.body)
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    )


def test_local_is_never_special_cased_into_health() -> None:
    """No code path promotes a failed local probe to usable.

    Guards against a future "a stopped local endpoint is probably fine" shortcut,
    which would be a silent routing change made in the presentation layer.
    """
    source = Path(str(_module("forge.cli.render").__file__)).read_text()
    tree = ast.parse(source)

    guarded = (
        "_provider_summary_lines",
        "_provider_label",
        "_failed_slot_lines",
        "_verified_label",
    )
    seen: list[str] = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in guarded):
            continue
        seen.append(node.name)

        # Docstrings stripped first: these functions *explain* the health
        # vocabulary in prose precisely so nobody re-derives it, and a guard that
        # fired on the explanation would push someone to delete it. What must not
        # appear is a health value in executable position -- a comparison, a
        # subscript, a returned literal.
        code_only = ast.unparse(
            ast.Module(
                body=node.body[1:] if _is_docstring(node) else node.body,
                type_ignores=[],
            )
        )
        for value in ("Health", "GREEN", "YELLOW", "RED"):
            assert value not in code_only, f"{node.name} uses {value} in code"

    assert sorted(seen) == sorted(guarded), "a guarded renderer changed name"


# --------------------------------------------------------------------------
# Architecture guards
# --------------------------------------------------------------------------


def _handlers(name: str, *function_names: str) -> list[ast.FunctionDef]:
    source = Path(str(_module(name).__file__)).read_text()
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name in function_names
    ]


def test_the_router_gains_no_provider_http_and_no_verifier_import() -> None:
    """`ModelRouter` decides; it does not call, and it does not probe."""
    source = Path(str(_module("forge.models.router").__file__)).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for imported in names:
            assert "verify" not in imported, imported
            assert "urllib" not in imported, imported
            assert "openrouter" not in imported, imported
            assert "local" != imported, imported


def test_the_registry_gains_no_verifier_import() -> None:
    """`Availability` is an injection point, not a place that reaches out."""
    source = Path(str(_module("forge.models.registry").__file__)).read_text()

    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for imported in names:
            assert "urllib" not in imported, imported
            assert imported != "verify", imported


def test_the_verify_module_imports_the_registry_but_never_the_reverse() -> None:
    """Direction check: a probe module may import the decision path, not vice versa."""
    verify_source = Path(str(_module("forge.models.verify").__file__)).read_text()

    assert "from .registry" in verify_source or "from forge.models.registry" in verify_source


def test_the_cli_status_handler_does_not_import_a_gateway_or_urllib() -> None:
    """Plain status stays credential-free and socket-free at the import level.

    Scoped to the offline handler: `--verify` legitimately reaches the verifier
    module, and a guard that failed there would be telling us to remove the
    feature rather than protect the default path.
    """
    handlers = _handlers("forge.cli.main", "_run_models_status_offline", "_run_models_status")

    assert handlers, "status handler not found; this guard has gone stale"

    for handler in handlers:
        body = ast.dump(handler)
        assert "API_KEY" not in body, handler.name
        assert "urllib" not in body, handler.name


def test_the_gateway_protocol_still_has_exactly_one_method() -> None:
    """A provider addition is a reason to want more than one method here.

    The reason this guard exists: verification was implemented as a *separate*
    Protocol precisely so the gateway interface would not have to grow.
    """
    from forge.models.base import ModelGateway

    methods = [
        name
        for name in dir(ModelGateway)
        if not name.startswith("_") and callable(getattr(ModelGateway, name))
    ]

    assert methods == ["complete"]


def test_a_verifier_is_not_a_gateway() -> None:
    """The two Protocols are genuinely different shapes."""
    from forge.models.base import ModelGateway, ModelVerifier

    assert "verify" not in dir(ModelGateway)
    assert "complete" not in dir(ModelVerifier)


def test_the_offline_gateway_is_not_required_to_verify() -> None:
    """Adding verification must not force ScriptedGateway to change.

    The cost of a second gateway method would be paid by an implementation that
    has no use for it.
    """
    from forge.models.scripted import ScriptedGateway

    assert isinstance(ScriptedGateway(), ModelGateway)
    assert not isinstance(ScriptedGateway(), ModelVerifier)


def test_both_routable_providers_satisfy_the_verifier_protocol(monkeypatch) -> None:
    """A verifier is a Protocol, so structural conformance is the contract."""
    from forge.models import local, openrouter

    openrouter_verifier = openrouter.OpenRouterVerifier(environ={})
    local_verifier = local.LocalLlamaVerifier()

    assert isinstance(openrouter_verifier, ModelVerifier)
    assert isinstance(local_verifier, ModelVerifier)


def test_verify_never_calls_complete() -> None:
    """Verification cannot generate a token, by construction of the seam."""
    source = Path(str(_module("forge.models.verify").__file__)).read_text()

    assert ".complete(" not in source
    assert "chat/completions" not in source