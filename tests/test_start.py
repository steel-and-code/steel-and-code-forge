"""Tests for `forge start` -- the one-command Space Monkey + Workbench launch.

Two layers are exercised:

*   the orchestration layer (``forge.startup.orchestrator``): configuration
    loading, container start, tunnel reuse/ownership, model validation, and the
    inference smoke test -- with subprocess and network seams patched so no test
    needs the real host;
*   the CLI boundary (``_run_start``): that the provider is forced to ``local``,
    the model is pinned, a failed bring-up exits ``EXIT_MODEL_UNAVAILABLE`` with
    a recovery pointer, and a bad configuration exits ``EXIT_USAGE`` without
    touching the network.

One test (``test_smoke_inference_round_trips_against_http_server``) runs the
real ``LocalLlamaGateway`` and ``LocalLlamaVerifier`` against a live in-process
HTTP server, which is a genuine inference round trip and not a mock of it.
"""

from __future__ import annotations

import http.server
import importlib
import io
import json
import subprocess
import threading

import pytest

import forge.startup.orchestrator as orch
from forge.cli import render
from forge.cli.main import main
from forge.models.base import ModelListing
from forge.startup.orchestrator import (
    StartError,
    SpaceMonkeyConfig,
    Tunnel,
    bring_up,
    ensure_container,
    ensure_tunnel,
    load_config,
    smoke_inference,
    validate_model,
)


def _config(**overrides: object) -> SpaceMonkeyConfig:
    base = dict(
        ssh_alias="byte-space-monkey",
        container="space-monkey",
        remote_port=11434,
        local_port=11435,
        model="qwen3.5:27b-q4_K_M",
    )
    base.update(overrides)
    return SpaceMonkeyConfig(**base)  # type: ignore[arg-type]


def _completed(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self._returncode = returncode
        self.terminated = False

    def poll(self) -> int | None:
        return self._returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode or 0

    def kill(self) -> None:
        self._returncode = -9


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_load_config_defaults() -> None:
    config = load_config({})
    assert config.ssh_alias == "byte-space-monkey"
    assert config.container == "space-monkey"
    assert config.remote_port == 11434
    assert config.local_port == 11435
    assert config.model == "qwen3.5:27b-q4_K_M"
    assert config.chat_endpoint == "http://127.0.0.1:11435/v1/chat/completions"
    assert config.models_endpoint == "http://127.0.0.1:11435/v1/models"


def test_load_config_overrides() -> None:
    config = load_config(
        {
            "FORGE_START_SSH_ALIAS": "other-host",
            "FORGE_START_CONTAINER": "ollama",
            "FORGE_START_REMOTE_PORT": "21434",
            "FORGE_START_LOCAL_PORT": "31435",
            "FORGE_START_MODEL": "qwen3.5:9b",
        }
    )
    assert config.ssh_alias == "other-host"
    assert config.container == "ollama"
    assert config.remote_port == 21434
    assert config.local_port == 31435
    assert config.model == "qwen3.5:9b"
    assert config.chat_endpoint == "http://127.0.0.1:31435/v1/chat/completions"


@pytest.mark.parametrize("bad", ["notaport", "0", "70000", "-1"])
def test_load_config_rejects_bad_port(bad: str) -> None:
    with pytest.raises(StartError, match="port number"):
        load_config({"FORGE_START_LOCAL_PORT": bad})


# --------------------------------------------------------------------------
# Container
# --------------------------------------------------------------------------


def test_ensure_container_is_noop_when_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_container_running", lambda config: True)

    def _fail(*a: object, **k: object) -> None:
        raise AssertionError("`_run` must not be called when the container is up")

    monkeypatch.setattr(orch, "_run", _fail)
    assert ensure_container(_config()) is None


def test_ensure_container_starts_when_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter([False, True])
    monkeypatch.setattr(orch, "_container_running", lambda config: next(states))

    seen: list[list[str]] = []

    def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        seen.append(argv)
        return _completed(0)

    monkeypatch.setattr(orch, "_run", _run)
    ensure_container(_config())
    assert seen, "podman start was never issued"
    assert any("start" in argv for argv in seen)


def test_ensure_container_start_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_container_running", lambda config: False)
    monkeypatch.setattr(orch, "_run", lambda argv, timeout: _completed(1, stderr="boom"))
    with pytest.raises(StartError, match="boom"):
        ensure_container(_config())


def test_ensure_container_not_up_after_start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_container_running", lambda config: False)
    monkeypatch.setattr(orch, "_run", lambda argv, timeout: _completed(0))
    with pytest.raises(StartError, match="did not come up"):
        ensure_container(_config())


# --------------------------------------------------------------------------
# Tunnel
# --------------------------------------------------------------------------


def test_ensure_tunnel_reuses_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_listing_available", lambda config: True)

    def _no_popen(*a: object, **k: object) -> None:
        raise AssertionError("no tunnel should be opened when one is already healthy")

    monkeypatch.setattr(orch.subprocess, "Popen", _no_popen)
    tunnel = ensure_tunnel(_config())
    assert tunnel == Tunnel(process=None, owned=False)


def test_ensure_tunnel_errors_on_occupied_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_listing_available", lambda config: False)
    monkeypatch.setattr(orch, "_port_open", lambda port: True)
    with pytest.raises(StartError, match="in use but not answering"):
        ensure_tunnel(_config())


def test_ensure_tunnel_opens_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = iter([False, True])
    monkeypatch.setattr(orch, "_listing_available", lambda config: next(listing))
    monkeypatch.setattr(orch, "_port_open", lambda port: False)
    monkeypatch.setattr(orch.time, "sleep", lambda seconds: None)

    process = _FakeProcess(None)
    monkeypatch.setattr(orch.subprocess, "Popen", lambda *a, **k: process)

    tunnel = ensure_tunnel(_config())
    assert tunnel.owned is True
    assert tunnel.process is process


def test_ensure_tunnel_exits_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_listing_available", lambda config: False)
    monkeypatch.setattr(orch, "_port_open", lambda port: False)
    monkeypatch.setattr(orch.subprocess, "Popen", lambda *a, **k: _FakeProcess(255))
    with pytest.raises(StartError, match="exited immediately"):
        ensure_tunnel(_config())


def test_ensure_tunnel_timeout_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "TUNNEL_READY_TIMEOUT", -1.0)
    monkeypatch.setattr(orch, "_listing_available", lambda config: False)
    monkeypatch.setattr(orch, "_port_open", lambda port: False)

    process = _FakeProcess(None)
    monkeypatch.setattr(orch.subprocess, "Popen", lambda *a, **k: process)

    with pytest.raises(StartError, match="did not become ready"):
        ensure_tunnel(_config())
    assert process.terminated is True, "a tunnel this run opened must be cleaned up"


# --------------------------------------------------------------------------
# Model validation
# --------------------------------------------------------------------------


def _fake_verifier(listing: ModelListing) -> object:
    class _Verifier:
        def list_models(self) -> ModelListing:  # noqa: PLR6301 - fake
            return listing

    return _Verifier()


def test_validate_model_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = ModelListing(provider="local", models=("a", "b"), available=True)
    monkeypatch.setattr(orch, "LocalLlamaVerifier", lambda **k: _fake_verifier(listing))
    assert validate_model(_config(model="a")) == ["a", "b"]


def test_validate_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = ModelListing(provider="local", available=False, error="refused")
    monkeypatch.setattr(orch, "LocalLlamaVerifier", lambda **k: _fake_verifier(listing))
    with pytest.raises(StartError, match="not reachable"):
        validate_model(_config())


def test_validate_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = ModelListing(provider="local", models=("qwen3.5:9b",), available=True)
    monkeypatch.setattr(orch, "LocalLlamaVerifier", lambda **k: _fake_verifier(listing))
    with pytest.raises(StartError, match="qwen3.5:9b"):
        validate_model(_config(model="qwen3.5:27b-q4_K_M"))


# --------------------------------------------------------------------------
# Smoke inference
# --------------------------------------------------------------------------


def _post(monkeypatch: pytest.MonkeyPatch, status: int, body: str) -> None:
    monkeypatch.setattr(orch, "_post_completion", lambda endpoint, payload, timeout: (status, body))


def test_smoke_inference_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _post(monkeypatch, 200, '{"choices":[{"message":{"content":"ready"}}]}')
    assert smoke_inference(_config()) is None


def test_smoke_inference_reasoning_only_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # A thinking model (Qwen's default) emits reasoning, not content: that is
    # still proof of inference, so the smoke test must accept it.
    _post(monkeypatch, 200, '{"choices":[{"message":{"content":"","reasoning":"Thinking..."}}]}')
    assert smoke_inference(_config()) is None


def test_smoke_inference_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _post(monkeypatch, 200, '{"choices":[{"message":{"content":"","reasoning":""}}]}')
    with pytest.raises(StartError, match="empty smoke-test reply"):
        smoke_inference(_config())


def test_smoke_inference_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _post(monkeypatch, 500, "")
    with pytest.raises(StartError, match="HTTP 500"):
        smoke_inference(_config())


def test_smoke_inference_unparseable(monkeypatch: pytest.MonkeyPatch) -> None:
    _post(monkeypatch, 200, "not json at all")
    with pytest.raises(StartError, match="unparseable"):
        smoke_inference(_config())


# --------------------------------------------------------------------------
# bring_up orchestration
# --------------------------------------------------------------------------


def test_bring_up_closes_owned_tunnel_on_later_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(None)
    owned = Tunnel(process=process, owned=True)
    calls: list[str] = []

    monkeypatch.setattr(orch, "ensure_container", lambda config: calls.append("container"))
    monkeypatch.setattr(
        orch, "ensure_tunnel", lambda config: (calls.append("tunnel") or owned)
    )
    monkeypatch.setattr(orch, "validate_model", lambda config: calls.append("validate"))

    def _fail(config: SpaceMonkeyConfig) -> None:
        raise StartError("the local model did not answer")

    monkeypatch.setattr(orch, "smoke_inference", _fail)

    with pytest.raises(StartError):
        bring_up(_config())
    assert calls == ["container", "tunnel", "validate"]
    assert process.terminated is True


def test_bring_up_returns_tunnel_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    reused = Tunnel(process=None, owned=False)
    monkeypatch.setattr(orch, "ensure_container", lambda config: None)
    monkeypatch.setattr(orch, "ensure_tunnel", lambda config: reused)
    monkeypatch.setattr(orch, "validate_model", lambda config: ["qwen3.5:27b-q4_K_M"])
    monkeypatch.setattr(orch, "smoke_inference", lambda config: None)

    assert bring_up(_config()) is reused


# --------------------------------------------------------------------------
# CLI boundary
# --------------------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def _start(argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, environ=env or {})
    return code, out.getvalue(), err.getvalue()


def test_start_dispatch_forces_local_provider_and_pins_model(
    monkeypatch: pytest.MonkeyPatch, ws
) -> None:
    monkeypatch.setattr(
        orch, "bring_up", lambda config, smoke: Tunnel(process=None, owned=False)
    )
    captured: dict[str, object] = {}

    def _fake_workbench(args, workspace, *, out, err, environ):
        captured["args_model"] = args.model
        captured["environ"] = environ
        return 0

    main_module = importlib.import_module("forge.cli.main")
    monkeypatch.setattr(main_module, "_run_workbench", _fake_workbench)

    code, _, err = _start(["start", "--workspace", str(ws)])
    assert code == 0, err
    assert captured["args_model"] == "qwen3.5:27b-q4_K_M"
    environ = captured["environ"]
    assert environ["FORGE_MODEL_PROVIDER"] == "local"
    assert environ["FORGE_LOCAL_ENDPOINT"] == "http://127.0.0.1:11435/v1/chat/completions"
    assert environ["FORGE_LOCAL_MODELS_ENDPOINT"] == "http://127.0.0.1:11435/v1/models"


def test_start_respects_explicit_model(monkeypatch: pytest.MonkeyPatch, ws) -> None:
    monkeypatch.setattr(
        orch, "bring_up", lambda config, smoke: Tunnel(process=None, owned=False)
    )
    captured: dict[str, object] = {}

    def _fake_workbench(args, workspace, *, out, err, environ):
        captured["args_model"] = args.model
        return 0

    main_module = importlib.import_module("forge.cli.main")
    monkeypatch.setattr(main_module, "_run_workbench", _fake_workbench)
    _start(["start", "--workspace", str(ws), "--model", "qwen3.5:9b"])
    assert captured["args_model"] == "qwen3.5:9b"


def test_start_bad_config_is_usage_error(ws) -> None:
    code, _, err = _start(
        ["start", "--workspace", str(ws)], {"FORGE_START_LOCAL_PORT": "notaport"}
    )
    assert code == render.EXIT_USAGE
    assert "port number" in err


def test_start_bringup_failure_is_model_unavailable(monkeypatch: pytest.MonkeyPatch, ws) -> None:
    def _fail(config, smoke):
        raise StartError("the local model endpoint is not reachable (refused)")

    monkeypatch.setattr(orch, "bring_up", _fail)
    code, _, err = _start(["start", "--workspace", str(ws)])
    assert code == render.EXIT_MODEL_UNAVAILABLE
    assert "could not start the local model" in err
    assert "docs/forge-start.md" in err


# --------------------------------------------------------------------------
# Real HTTP round trip
# --------------------------------------------------------------------------


class _ModelHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - HTTP verb
        if self.path == "/v1/models":
            body = json.dumps(
                {"object": "list", "data": [{"id": "qwen3.5:27b-q4_K_M"}, {"id": "qwen3.5:9b"}]}
            ).encode()
            self._respond(200, body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - HTTP verb
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps(
                {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}
            ).encode()
            self._respond(200, body)
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_smoke_and_validate_round_trip_against_http_server(local_server: int) -> None:
    config = _config(local_port=local_server, model="qwen3.5:27b-q4_K_M")

    # A real inference round trip through the actual local gateway.
    assert smoke_inference(config) is None

    # And the same endpoint's model listing validates through the real verifier.
    assert validate_model(config) == ["qwen3.5:27b-q4_K_M", "qwen3.5:9b"]