"""Bring up the Space Monkey local-model stack and hand off to the Workbench.

This module owns every subprocess and network concern of ``forge start`` so that
``forge/cli/main.py`` keeps importing neither -- for the same reason
``forge.external.runner`` exists: ``forge check`` must not be able to reach a
shell or a socket, and importing a module that can is exactly what would make it
able to. This module is therefore imported lazily, inside ``forge start``'s
dispatch, the same way the external-agent runner is imported inside
``forge hermes``.

The machine-specific facts -- the SSH alias, the container name, the remote and
local ports, and the default model -- are read from the environment, never
written into this module. Every value carries a default that matches the Space
Monkey host and is overridable, so this file is the same on any machine and the
configuration stays out of the application logic.

No credential is ever stored or logged. SSH runs with ``BatchMode=yes`` against
a host alias whose identity and key are held in ``~/.ssh/config``; the local
model endpoint takes no credential at all.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from ..models.local import LocalLlamaVerifier

__all__ = [
    "StartError",
    "SpaceMonkeyConfig",
    "Tunnel",
    "load_config",
    "bring_up",
    "close_tunnel",
]

# The whole machine-specific configuration surface. Nothing else in Forge names
# a host, a container, or a port.
ENV_SSH_ALIAS = "FORGE_START_SSH_ALIAS"
ENV_CONTAINER = "FORGE_START_CONTAINER"
ENV_REMOTE_PORT = "FORGE_START_REMOTE_PORT"
ENV_LOCAL_PORT = "FORGE_START_LOCAL_PORT"
ENV_MODEL = "FORGE_START_MODEL"

DEFAULT_SSH_ALIAS = "byte-space-monkey"
DEFAULT_CONTAINER = "space-monkey"
DEFAULT_REMOTE_PORT = 11434
DEFAULT_LOCAL_PORT = 11435
DEFAULT_MODEL = "qwen3.5:27b-q4_K_M"

#: How long to wait for a freshly opened tunnel to answer before giving up.
TUNNEL_READY_TIMEOUT = 15.0

#: How long a remote ``podman`` command may take before it is considered stuck.
SSH_COMMAND_TIMEOUT = 30.0

#: The smoke prompt. A tiny, fixed completion that proves the model infers.
_SMOKE_PROMPT = "Reply with exactly one word: ready"

#: The smoke completion asks for no tool and bounds its output. Large enough
#: for a short answer but small enough that a wedged model does not run long.
_SMOKE_MAX_TOKENS = 64

#: How long one smoke completion may take before it is considered stuck.
SMOKE_TIMEOUT_SECONDS = 60.0

_SSH_BASE = ["ssh", "-o", "BatchMode=yes"]


class StartError(Exception):
    """``forge start`` could not bring the local-model stack up.

    The message is operator-safe: it names the step that failed and the fix to
    try, and never an ssh command line that a credential might live in.
    """


@dataclass(frozen=True)
class SpaceMonkeyConfig:
    """The machine-specific facts ``forge start`` needs, read from the environment."""

    ssh_alias: str
    container: str
    remote_port: int
    local_port: int
    model: str

    @property
    def chat_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.local_port}/v1/chat/completions"

    @property
    def models_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.local_port}/v1/models"


@dataclass(frozen=True)
class Tunnel:
    """An SSH tunnel: reused from another process, or opened by this run.

    ``owned`` distinguishes the two. ``close_tunnel`` terminates only a tunnel
    this run created, so a pre-existing tunnel is never killed out from under
    its owner.
    """

    process: subprocess.Popen | None
    owned: bool


def _port(name: str, raw: str) -> int:
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        raise StartError(f"{name} must be a port number (1-65535); it is not a number") from None
    if not 1 <= value <= 65535:
        raise StartError(f"{name} must be a port number (1-65535); got {value}") from None
    return value


def _text(name: str, raw: str, default: str) -> str:
    del name  # named for future validation symmetry; kept uniform
    return (raw or "").strip() or default


def load_config(environ: Mapping[str, str]) -> SpaceMonkeyConfig:
    """Read the Space Monkey configuration, failing clearly on a bad value.

    A blank value means "use the default", matching the local endpoint's rule
    that an empty export behaves like an unset one.
    """
    return SpaceMonkeyConfig(
        ssh_alias=_text(
            ENV_SSH_ALIAS, environ.get(ENV_SSH_ALIAS, ""), DEFAULT_SSH_ALIAS
        ),
        container=_text(
            ENV_CONTAINER, environ.get(ENV_CONTAINER, ""), DEFAULT_CONTAINER
        ),
        remote_port=_port(
            ENV_REMOTE_PORT, environ.get(ENV_REMOTE_PORT, str(DEFAULT_REMOTE_PORT))
        ),
        local_port=_port(
            ENV_LOCAL_PORT, environ.get(ENV_LOCAL_PORT, str(DEFAULT_LOCAL_PORT))
        ),
        model=_text(ENV_MODEL, environ.get(ENV_MODEL, ""), DEFAULT_MODEL),
    )


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    # ``subprocess.run`` is looked up at call time so tests can patch it.
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _ssh(config: SpaceMonkeyConfig, *remote: str) -> list[str]:
    return [*_SSH_BASE, config.ssh_alias, *remote]


def _fail_running(step: str, exc: BaseException) -> StartError:
    if isinstance(exc, subprocess.TimeoutExpired):
        return StartError(f"timed out {step} over SSH")
    if isinstance(exc, OSError):
        return StartError(
            f"{step}: {exc.__class__.__name__}; is `ssh` installed and the "
            "Space Monkey alias configured?"
        )
    return StartError(f"{step}: {exc}")


def _container_running(config: SpaceMonkeyConfig) -> bool:
    """Whether the named container is reported ``Up`` on the remote host."""
    # The Go template contains a space, which must survive ssh's argument
    # joining as ONE remote argument. `shlex.quote` wraps it so the remote
    # shell keeps it together; without it the remote sees two argv and podman
    # rejects the stray positional ("`podman ps` takes no arguments").
    fmt = shlex.quote("{{.Names}} {{.Status}}")
    argv = _ssh(config, "podman", "ps", "--format", fmt)
    try:
        completed = _run(argv, SSH_COMMAND_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _fail_running("checking the Space Monkey container", exc) from None
    if completed.returncode != 0:
        raise StartError(
            f"the Space Monkey host rejected the container query "
            f"(exit {completed.returncode}): {completed.stderr.strip()}"
        )
    for line in completed.stdout.splitlines():
        parts = line.split(None, 1)
        if parts and parts[0] == config.container and len(parts) > 1:
            return parts[1].startswith("Up")
    return False


def ensure_container(config: SpaceMonkeyConfig) -> None:
    """Start the Ollama container if it is not already running.

    ``podman start`` is idempotent, but we check first so "already running" is a
    fast no-op rather than a round trip -- and so we can say clearly when it
    starts versus when it was up already.
    """
    if _container_running(config):
        return
    argv = _ssh(config, "podman", "start", config.container)
    try:
        completed = _run(argv, SSH_COMMAND_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _fail_running(f"starting the {config.container!r} container", exc) from None
    if completed.returncode != 0:
        raise StartError(
            f"could not start the {config.container!r} container on the Space "
            f"Monkey host: {completed.stderr.strip()}"
        )
    if not _container_running(config):
        raise StartError(
            f"the {config.container!r} container did not come up after "
            "`podman start`; check `podman logs` on the host"
        )


def _port_open(port: int) -> bool:
    """Whether something accepts a TCP connection on ``port``.

    Distinct from model health: a port can be open without serving a model,
    which is exactly the "stale tunnel / wrong process" state ``forge start``
    must refuse to duplicate.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _listing_available(config: SpaceMonkeyConfig) -> bool:
    """Whether the local port is answering as a model endpoint right now."""
    verifier = LocalLlamaVerifier(endpoint=config.models_endpoint)
    return verifier.list_models().available


def ensure_tunnel(config: SpaceMonkeyConfig) -> Tunnel:
    """Reuse a healthy tunnel, or open one; never duplicate and never steal.

    Three outcomes, in order:

    * the local port already serves a model listing -> reuse (owned=False);
    * the port is occupied but does not serve a model -> fail, naming the fix;
    * the port is free -> open an ssh tunnel and wait for it to answer
      (owned=True).
    """
    if _listing_available(config):
        return Tunnel(process=None, owned=False)

    if _port_open(config.local_port):
        raise StartError(
            f"port {config.local_port} is in use but not answering as a local "
            f"model; stop the process holding it or choose another local port "
            f"via {ENV_LOCAL_PORT}"
        )

    argv = [
        *_SSH_BASE,
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-L",
        f"{config.local_port}:127.0.0.1:{config.remote_port}",
        config.ssh_alias,
    ]
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise StartError(f"could not start the SSH tunnel: {exc.__class__.__name__}") from None

    deadline = time.monotonic() + TUNNEL_READY_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StartError(
                "the SSH tunnel exited immediately "
                f"(exit {process.returncode}); a port may already be taken "
                "on either side"
            )
        if _listing_available(config):
            return Tunnel(process=process, owned=True)
        time.sleep(0.25)

    close_tunnel(Tunnel(process=process, owned=True))
    raise StartError(
        f"the SSH tunnel did not become ready within {TUNNEL_READY_TIMEOUT:g}s"
    )


def validate_model(config: SpaceMonkeyConfig) -> list[str]:
    """Confirm the selected model is served; raise with the list otherwise."""
    verifier = LocalLlamaVerifier(endpoint=config.models_endpoint)
    listing = verifier.list_models()
    if not listing.available:
        raise StartError(
            f"the local model endpoint at 127.0.0.1:{config.local_port} is not "
            f"reachable ({listing.error or 'unknown error'})"
        )
    models = sorted(listing.models)
    if config.model not in listing.models:
        raise StartError(
            f"the local endpoint is serving {', '.join(models) or 'no models'}, "
            f"but not {config.model!r}; pull it on the host, or select another "
            f"with --model / {ENV_MODEL}"
        )
    return models


def _post_completion(
    endpoint: str, payload: Mapping[str, object], timeout: float
) -> tuple[int, str]:
    """One OpenAI-compatible completion POST, returning ``(status, body)``.

    Raises :class:`StartError` on a transport failure; returns the HTTP status
    for anything else so the caller can report it without unpacking an
    exception body.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = ""
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the status alone is enough to report
            text = ""
        return int(exc.code), text
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise StartError(
            f"the local model endpoint did not answer ({type(reason).__name__})"
        ) from None


def _completion_has_output(payload: object) -> bool:
    """Whether a completion carried any assistant output.

    A "thinking" local model (Qwen's default) emits its work in a ``reasoning``
    field and leaves ``content`` empty, so inference is proven by either field,
    not ``content`` alone.
    """
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return False
    return any(
        bool(str(message.get(field, "") or "").strip())
        for field in ("content", "reasoning")
    )


def smoke_inference(config: SpaceMonkeyConfig) -> None:
    """Run one bounded completion to prove the model actually infers.

    Listing a model is not serving it; only a real round trip shows the model
    answers. Fails (rather than returning silently) so ``forge start`` never
    launches a Workbench pointed at a model that merely exists.
    """
    status, text = _post_completion(
        config.chat_endpoint,
        {
            "model": config.model,
            "messages": [{"role": "user", "content": _SMOKE_PROMPT}],
            "max_tokens": _SMOKE_MAX_TOKENS,
        },
        SMOKE_TIMEOUT_SECONDS,
    )
    if status != 200:
        raise StartError(
            f"the local model returned HTTP {status} to a smoke prompt"
        )
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        raise StartError(
            "the local model returned an unparseable smoke-test reply"
        ) from None
    if not _completion_has_output(parsed):
        raise StartError(
            "the local model returned an empty smoke-test reply "
            "(no content and no reasoning)"
        )


def bring_up(config: SpaceMonkeyConfig, *, smoke: bool = True) -> Tunnel:
    """Start the container, open or reuse the tunnel, and verify the model.

    Returns the tunnel so the caller can close it when the Workbench exits.
    Raises :class:`StartError` on any failure, always before the Workbench is
    launched, and closes a tunnel it opened if a later step fails.
    """
    ensure_container(config)
    tunnel = ensure_tunnel(config)
    try:
        validate_model(config)
        if smoke:
            smoke_inference(config)
    except BaseException:
        close_tunnel(tunnel)
        raise
    return tunnel


def close_tunnel(tunnel: Tunnel | None) -> None:
    """Terminate a tunnel this run opened; never one owned elsewhere."""
    if tunnel is None or not tunnel.owned or tunnel.process is None:
        return
    process = tunnel.process
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except OSError:
        pass