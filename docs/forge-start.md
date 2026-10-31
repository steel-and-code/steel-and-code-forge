# `forge start` — one-command Workbench on Space Monkey

`forge start` launches the Forge Workbench on the Space Monkey local model in
one command. It starts the remote Ollama container if needed, opens (or
reuses) the SSH tunnel, verifies the selected model with a smoke inference,
then serves the same Workbench `forge workbench` serves — pinned to the local
provider, so it never silently routes to a paid model.

```sh
forge start --workspace /path/to/project
forge start --workspace /path/to/project --model qwen3.5:9b
forge start --workspace /path/to/project --no-smoke
```

## Configuration

Every machine-specific fact is read from the environment, with defaults that
match the Space Monkey host. Nothing here is stored in code, and no credential
is ever stored or logged.

| Variable | Default | Meaning |
|---|---|---|
| `FORGE_START_SSH_ALIAS` | `byte-space-monkey` | SSH host alias (`~/.ssh/config`) |
| `FORGE_START_CONTAINER` | `space-monkey` | Remote Podman container running Ollama |
| `FORGE_START_REMOTE_PORT` | `11434` | Ollama port on the remote host |
| `FORGE_START_LOCAL_PORT` | `11435` | Local tunnel port (must be free) |
| `FORGE_START_MODEL` | `qwen3.5:27b-q4_K_M` | Model served to the Workbench |

The two local endpoints are derived from `FORGE_START_LOCAL_PORT` and passed to
the existing local gateway as `FORGE_LOCAL_ENDPOINT` and
`FORGE_LOCAL_MODELS_ENDPOINT`. The provider is always forced to `local`.

## What it checks, in order

1. `ssh` is usable and the host alias answers.
2. The `space-monkey` container is `Up`; if not, it runs `podman start` (idempotent).
3. A healthy tunnel already exists → reuse it. Port occupied but not serving a
   model → fail (no duplicate tunnel). Port free → open `ssh -N -L`.
4. The model is listed by the endpoint's `/v1/models`.
5. One smoke inference round-trips (skip with `--no-smoke`).

Tunnel ownership: a tunnel `forge start` opened is terminated when the
Workbench exits (Ctrl-C). A tunnel opened by another process is reused and left
alone.

## Recovery

`forge start` fails before the Workbench launches and prints the first step
that failed. Match it here.

| Symptom | Cause | Fix |
|---|---|---|
| `timed out ... over SSH` / `could not query the Space Monkey host` | Host unreachable or alias wrong | `ssh byte-space-monkey hostname`; check the alias and key in `~/.ssh/config` |
| `could not start the 'space-monkey' container` | Container stopped or broken | `ssh <alias> podman logs space-monkey`; `podman start space-monkey` by hand |
| `did not come up after podman start` | Container starts but Ollama fails | `ssh <alias> podman logs space-monkey` for the crash |
| `port 11435 is in use but not answering` | Stale/foreign process on the tunnel port | `ss -ltnp | grep 11435`, kill the stale `ssh`, or set `FORGE_START_LOCAL_PORT` |
| `SSH tunnel exited immediately` | Port taken on either side | Free the port, or change `FORGE_START_LOCAL_PORT` / `FORGE_START_REMOTE_PORT` |
| `not serving ... but not <model>` | Model not pulled on the host | `ssh <alias> podman exec space-monkey ollama pull <model>`, or `--model qwen3.5:9b` |
| `did not answer a smoke prompt` | Model not loaded / server error | `ssh <alias> podman exec space-monkey ollama ps`; retry without `--no-smoke` once loaded |

If a tunnel is orphaned (its owner process died), the next `forge start`
detects it as an unhealthy port and refuses rather than duplicating — clear the
stale process, then re-run.