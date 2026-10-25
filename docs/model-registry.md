# Forge Model Registry and Routing

Status: implemented. Describes the contract, not the internals.

## Purpose

The registry answers **which models Forge has and which are usable**. The router
answers **which one serves a particular request**. They are separate layers
because they answer separate questions, and because a status view and a routing
decision that could disagree about health would make both untrustworthy.

The design constraint that shapes everything else:

> **Every model identifier in Forge is written in one configuration file and
> nowhere else.** An agent names a *capability*; configuration says which *model*
> serves it.

So `stealth/space-bunny-alpha` disappearing tomorrow is a configuration edit.
The agent that used it does not change, because it never named it.

## Layers

```
  forge/models/
    tiers.py       vocabulary: CapabilityTier, Health, SelectionKind
    base.py        ModelGateway, ModelVerifier, VerificationCode/Result
    providers.py   ProviderSpec, ProviderCatalog -- names, not classes
    registry.py    ModelSlot, TierConfig, RoutingTable, Availability,
                   ModelRegistry -- configuration and health
    router.py      RouteRequest, RouteDecision, ModelRouter -- selection
    verify.py      the probe: performs checks, returns Availability
    openrouter.py  OpenRouterGateway + OpenRouterVerifier
    local.py       LocalLlamaGateway + LocalLlamaVerifier
    models.toml    the packaged default registry
```

Dependencies run one way: `router -> registry -> providers -> tiers`, and
`verify -> registry -> base`. Nothing in the routing chain imports a gateway,
imports `forge.tools`, or opens a socket. Deciding is not calling, and probing
is a separate act from both.

## Local endpoint configuration

The `local` provider connects to an OpenAI‑compatible server on the local
machine — by default a llama.cpp server listening on port 8080, but also any
other service (Ollama, a tunnel, a proxy) that answers the same two HTTP paths.

Two environment variables select the endpoints independently, because a proxy
can serve the two paths on different prefixes, and because deriving one from
the other by string surgery is how a URL that happens to contain `/v1` twice
ends up pointed at the wrong place.

| Variable | Purpose | Default |
|---|---|---|
| `FORGE_LOCAL_ENDPOINT` | The `/v1/chat/completions` URL. | `http://127.0.0.1:8080/v1/chat/completions` |
| `FORGE_LOCAL_MODELS_ENDPOINT` | The `/v1/models` URL the health probe reads. | `http://127.0.0.1:8080/v1/models` |

**Both are optional.** A blank or absent value means “use the default”. An
explicit `endpoint` argument to `LocalLlamaGateway` or `LocalLlamaVerifier` wins
over the environment, because a caller passing a literal is stating a choice,
not being configured.

**Example: Space Monkey’s Ollama tunnel.** With Ollama reachable at
`http://127.0.0.1:11434` via an SSH tunnel, set:

```sh
export FORGE_LOCAL_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
export FORGE_LOCAL_MODELS_ENDPOINT=http://127.0.0.1:11434/v1/models
```

The registry can then route to `qwen3.5:4b` (or any other Ollama model) exactly
as it would to a llama.cpp model, with the same safety and approval boundaries.

## Capability tiers

A tier is **what a model is asked to do**, not what it is called. Three exist,
fixed in `tiers.py`:

| Tier | For |
|---|---|
| `general_light` | Cheap, fast work: summarise, triage, draft. |
| `coding_standard` | The workhorse: edit code, run tests, follow the tool loop. |
| `reasoning_heavy` | Architecture, cross-system debugging, anything needing depth. |

Tiers are the **stable contract**. The models behind them are not. Adding a
fourth tier is a code change, and deliberately so: it is a new concept, not a new
model. Replacing every model is a configuration edit.

A tier declares a **primary** and zero or more **fallbacks**, in order:

```toml
[tiers.coding_standard]
primary  = { model = "vendor/coder", provider = "openrouter" }
fallbacks = [
    { model = "qwen2.5-coder:14b", provider = "local" },
]
```

The primary is required even when disabled. A tier with no primary is a
configuration mistake rather than an empty-but-valid tier, because "which model
is tried first" is the question this layer exists to answer. Zero fallbacks is a
legitimate choice.

Each slot names its own provider. A tier's fallback may be a local model while
its primary is remote.

## Configured, enabled, available, verified

Four separate facts. A health display that collapses them into one boolean
lies, and the distinctions have different fixes:

| Fact | Meaning | Set by |
|---|---|---|
| **configured** | The slot exists in the configuration file. | Editing the file. |
| **enabled** | `enabled = true`. | An operator, by decision. |
| **available** | An availability source says it could serve now. | An injected source. |
| **verified** | Forge *asked the provider* and the provider offered this model. | A probe run. |

`SlotState` records the first three without hiding any of them:

```
CONFIGURED   present and enabled; nothing says it is unusable
DISABLED     present, enabled = false
UNAVAILABLE  enabled, but the availability source said no
UNROUTABLE   enabled and available, but its provider has no gateway
```

Precedence, and why:

- **disabled is checked before unavailable.** "You switched this off" is more
  useful than "this is also currently unreachable", and it names the thing an
  operator can actually change.
- **a disabled slot is never `UNROUTABLE`.** Reporting that a model nobody may
  use has no gateway is noise.
- **availability cannot re-enable a disabled model.** The operator's switch
  outranks any runtime probe. A probe may report a model healthy; it may never
  route to one an operator switched off.

"Configured" is not "available". An enabled slot with no availability source
reporting otherwise is `CONFIGURED` -- present and permitted, not confirmed
working.

### The assumed/default case, stated plainly

With no availability source, every enabled slot is treated as **available** and
the registry reports `verified = False`. That is a deliberate *assumption*, not
a finding:

- **Assumed availability** is what plain `forge models status` reports. It is
  enough to answer "what is configured and would it route", and it is worthless
  as an answer to "is my provider working". The status output says **unverified**
  in full for exactly this reason.
- **Verified availability** is what `forge models status --verify` reports. It
  costs one bounded request per distinct model and answers the second question.

> **The two must never be presented in the same column.** A GREEN row whose
> VERIFIED cell is empty means "Forge would route here and has confirmed
> nothing". Printing `yes` there is the single failure mode this design exists
> to prevent.

### What "verified" does and does not claim

Verified means **one bounded request was made and the provider offered this
model**. Specifically it does *not* claim:

- that a completion would succeed (nothing is generated during verification);
- that the provider is healthy in general;
- that the credential will keep working;
- anything at all about a slot that was not probed.

A probe that found every model broken still reports `verified = True`, because
*checked* is what the word means. The failure is in the answer, not the check,
and conflating the two would make a total outage look like never having looked.

## Health

Computed from **usability**, never from configuration presence.

| Health | Meaning |
|---|---|
| **GREEN** | The primary is usable. Traffic flows as configured. |
| **YELLOW** | The primary is not usable; at least one fallback is. Traffic still flows, but cost or quality may have changed. |
| **RED** | Nothing in the tier is usable. No traffic is served. |

Three-valued rather than boolean on purpose: an operator must tell "serving
normally" from "degraded but alive" from "cannot serve", and a boolean
collapses the first two -- the two that matter most when something has broken.

A tier the registry does not carry reports **RED**, not an error and not a
sentinel. A status consumer iterating known tier names always receives a value
and never has to decide what absence means.

**Loading a configuration is not checking a provider.** A tier whose only model
sits on a provider with no gateway implementation is RED even though the file
parsed without complaint. A green light means something could serve, not
something did.

**Verification does not redefine these three.** It only supplies better facts
underneath them. `TierConfig.health()` is the single definition and it was not
touched; `--verify` feeds it through `Availability` and changes nothing about
what GREEN means.

## Primary and fallback behaviour

The router walks a tier in declaration order: primary, then each fallback.

- Primary usable -> selected, `selection = primary`, health GREEN.
- Primary not usable -> first usable fallback, `selection = fallback`, health
  YELLOW. Fallback position is recorded and reported: the first and third
  fallback are different cost and behaviour.
- Nothing usable -> `selection = none`, health RED.

Every decision carries a **reason** naming the specific slot and its specific
state:

```
primary 'openrouter/space-bunny-alpha' is disabled in configuration;
selected fallback #1 'openrouter/vendor/newcoder', which is enabled and available
```

**A routing failure is a decision value, never an exception.** An operator
inspecting routing wants to be told "nothing can serve tier X, primary disabled,
0 of 2 fallbacks usable" -- not handed a traceback from an inspection command.
It exits `EXIT_MODEL_UNAVAILABLE` (5), deliberately distinct from
`EXIT_REFUSED` (1): "nothing to run" and "not allowed" are different facts, and a
script branching on either should not conflate them.

Routing never substitutes across tiers. Serving `general_light` traffic from
`coding_standard` would answer a different question than the one asked.

## Provider abstraction

A provider is a **name and a credential variable name**, not a class:

```python
ProviderSpec(name="openrouter",
             credential_env="FORGE_OPENROUTER_API_KEY",
             gateway_factory=...,      # or None
             verifier_factory=...)     # or None
```

`forge.models.registry` and `forge.models.router` import no gateway. Providers
are written down in exactly one place, `ProviderCatalog.default()`.

`routable` and `verifiable` are separate facts. A provider can be one without
the other, and treating them as the same is how "we could have asked but did
not" becomes a confident green.

| Provider | Routable | Verifiable | Credential |
|---|---|---|---|
| `openrouter` | yes | yes | `FORGE_OPENROUTER_API_KEY` |
| `local` | yes | yes | none |
| `openai` | no | no | `FORGE_OPENAI_API_KEY` |
| `anthropic` | no | no | `FORGE_ANTHROPIC_API_KEY` |

The asymmetry is deliberate. Declaring `openai` and `anthropic` means a
configuration naming them **validates** and `forge models status` can show the
slot honestly, rather than it being silently dropped. Leaving them without a
gateway means routing to one returns a structured failure naming the provider --
never an import error inside a run. An unimplemented provider is *declared but
unroutable*, which is why such a tier is RED rather than reassuring.

`--verify` reports the same asymmetry through the same mechanism: a slot on an
unimplemented provider comes back `verify_code = unimplemented`, a clean result
naming the provider. Nothing is contacted and nothing raises.

A provider redeclared in `models.toml` **keeps** its gateway and verifier
implementations; only the credential name is configurable. Dropping them on
redeclaration would turn a provider Forge can check into one it reports as
unimplemented -- the exact failure `--verify` exists to remove.

The catalog holds credential **variable names** only. It does not read a
credential, does not import `os` to do so, and cannot leak one. Values are read
by the gateway layer alone.

A provider Forge has never heard of is configuration, not a code change:

```toml
[providers.house]
credential_env = "FORGE_HOUSE_KEY"
```

## Routing policy

The task/difficulty to tier mapping lives in configuration, so difficulty is an
operator decision:

```toml
[routing]
default_tier = "general_light"

[routing.tasks.coding]
light     = "general_light"
standard  = "coding_standard"
hard      = "reasoning_heavy"
```

Resolution, most specific first:

1. `routing.tasks.<task>.<difficulty>`
2. `routing.tasks.<task>.standard` -- an unlisted difficulty
3. `routing.default_tier` -- an unlisted task, or no task

An unanticipated task **still routes**. Refusing would make the completeness of
a configuration file load-bearing for the whole system, which is the wrong
dependency. The decision records which rule matched
(`routing.tasks.coding.hard`), so an audit can tell a table hit from a default.

`RouteRequest.tier` overrides the table for an operator. It exists so an override
can be *stated*; it is recorded as `explicit request tier` rather than being made
to look like a table hit.

A request carries an agent name, recorded on the decision. It does not influence
selection: one agent receiving a different model from another is a policy
decision this layer is not entitled to make.

## Configuration resolution

TOML, read with stdlib `tomllib` -- no new dependency in a package that declares
zero runtime dependencies. First match wins:

1. `$FORGE_MODELS_CONFIG`
2. `./models.toml`
3. the packaged default (`forge/models/models.toml`, shipped as package data)

Configuration is **refused, not repaired**, at load time and never during a run: a
broken registry cannot be discovered halfway through a task. Unrecognised
top-level tables are refused rather than ignored -- a silently dropped typo turns
a working configuration into a no-op that still reports healthy.

Error messages name the offending table and the rule it broke, and never file
contents.

## The verified-health seam

`Availability` is the injection point. It accepts a mapping, a callable, or the
results of a probe, and performs **no network I/O**:

```python
Availability(source={"local/qwen2.5-coder:14b": False})   # by provider/model
Availability(source=lambda slot: ...)                      # for an injected rule
Availability.verified_results(results)                     # from a real probe
Availability.none()                                       # nothing is known
```

`Availability.verified` records whether anything actually checked. It is
**False** unless a real source was supplied.

> **Absence from an availability mapping must never later be presented as
> verified healthy.** A model missing from a mapping means *not known to be
> broken*, which is not the same as *confirmed working*. It counts as usable for
> routing, and the registry reports the result as **unverified** -- a term
> `forge models status` prints in full rather than abbreviating.

Requiring a mapping to enumerate every model would mean adding a fallback is a
two-place edit and marks a newly configured model broken by default. Failing in
that direction is the dangerous one, so absence means available and the
distinction stays visible instead of being resolved by pessimism.

The probe drops into this same seam through `registry.with_availability(...)`;
the registry is immutable, so it returns a new value rather than mutating the
one a caller holds. **A probe module imports this one; this one never imports a
probe.** The decision path must not be able to make a network call, and
`tests/test_model_verify.py` asserts that structurally rather than by review.

Per-slot reasons and codes travel with the source. `Availability.reason_for()`
and `Availability.code_for()` are what a status consumer reads, so it never has
to pattern-match on prose to tell an auth failure from a timeout.

Credentials are deliberately *not* consulted for health **on the offline path**.
A missing key is configuration, not evidence a model is unreachable. Under
`--verify` the credential is used, but only by the verifier, and a missing one
comes back as `verify_code = credential_missing` -- a reportable status, not an
exception.

## Verified health: `forge models status --verify`

### What it does

```
forge models status --verify
```

One bounded request per **distinct** `provider/model`, deduplicated across
tiers, then health reported through the existing computation. The results enter
health as an ordinary `Availability`, so `TierConfig.health()` decides
GREEN/YELLOW/RED exactly as before.

### Verification strategy, per provider

| Provider | Check | Cost | Failure mapping |
|---|---|---|---|
| `openrouter` | `GET /api/v1/models` with the key in the `Authorization` header; model id present in the listing means usable | free; no tokens generated | 401/403 `unauthenticated`; no key `credential_missing`; socket timeout `timeout`; 200 but id absent `not_found`; non-JSON `malformed_response`; connection refused `unreachable` |
| `local` | `GET /v1/models` on the OpenAI-compatible endpoint | free; loopback | as above, no auth branch |
| `openai`, `anthropic` | none | none | `unimplemented` |

**Why a listing and not a completion.** A one-token completion would additionally
prove the model *answers*, at the price of spending money on every status check
and generating text nobody asked for. The listing is the cheapest check that
reliably establishes usability, and it cannot be billed.

### Bounds, by construction

- One attempt per distinct model. No retries, no backoff -- a verification loop
  is an outage amplifier.
- A five-second per-attempt timeout, separate from a call's own timeout.
- **No completion, ever.** There is no code path from `verify.py` to
  `ModelGateway.complete`, and a test asserts the string `chat/completions` does
  not appear in that module.
- No agent runtime, no workspace, no approval. A probe cannot execute anything;
  it asks a provider about a list.
- A **disabled slot is never probed.** An operator's switch is not something a
  probe second-guesses, and spending a request on a switched-off model would
  produce a result that could read as coverage.

### The verification summary

After the tier table, `--verify` prints one line per provider:

```
  verification summary
  OpenRouter    2 checked, 1 healthy
  Local         1 checked, 0 healthy
  Anthropic     0 checked, 1 not checked, no health information
```

**Presentation and diagnostics only.** It is computed in `render.py` by counting
the slot rows already in `status()`. No health value, routing decision, or exit
code is derived from it, and no such value reads from it. A test asserts the
four renderer helpers that produce the diagnostics reference no health constant
in executable position.

It exists because the question an operator asks after reading the table is
"so is the problem my provider, or my laptop?", and a per-tier table cannot
answer that. The two providers above are visibly separate problems.

Two things it is careful about:

- **"checked" means a request was actually made.** A provider with no verifier
  reports `0 checked ... no health information`, because `unimplemented` is a
  result *value* rather than a request and counting it as checked would put a
  never-asked provider in the same column as one that answered and failed.
- **Models are counted once per provider**, deduplicated by `provider/model`, so
  the number agrees with the number of requests actually made. A model standing
  behind three tiers is one model.

## Operational wording for a stopped local endpoint

A refused connection on the local endpoint is a fact about the *machine*, not
about a model the provider cannot serve, and the two send an operator to
different places. The reason reads:

```
local endpoint unreachable; configured service may not be running (ConnectionRefusedError)
```

`OpenRouter endpoint unreachable; the provider could not be contacted (...)` is
the same shape for the hosted provider.

> **Operational wording is not a special case that forgives anything.** A
> stopped local endpoint is still an unusable model: the tier stays RED, the
> fallback is still not selected, and the exit code is still
> `EXIT_MODEL_UNAVAILABLE`. The wording changes how a failure *reads*, never
> whether it counts. There is no code path anywhere that promotes a failed local
> probe to usable, and one test asserts the renderer cannot introduce one.

### Failure is a value

Every failure mode returns a `VerificationResult` with a `VerificationCode` and
a bounded, operator-safe reason. A verifier that raises, returns a non-result, or
whose factory fails to build all become reportable statuses. An inspection
command that crashes tells an operator nothing they can act on.

Provider response bodies are **never** echoed into a reason. Providers quote the
offending request in their error prose, and that request carried the
authorization header. Only the HTTP status and short identifier fields survive;
transport failures report the exception *type* and never its message.

### Why verification is not a second gateway method

`ModelGateway` has exactly one method, and a test asserts it stays that way.
Verification is a separate, narrower `ModelVerifier` Protocol with a single
`verify(model)` method, reached through `ProviderSpec.verifier_factory`.

A verifier must never grow a completion path, a retry loop, or the ability to
generate a token -- and a shared interface would make enforcing that somebody
else's problem. `ScriptedGateway`, the offline implementation every test uses,
does not implement verification and is not required to.

## UI consumers must use `ModelRouter.status()`

> `ModelRouter.status()` is the **source of truth** for model health in any
> surface. A UI health indicator consumes it verbatim. It must not recompute
> health, re-read the registry, or reformat the fields.

`status()` returns one entry per capability tier -- every known tier name,
including one the registry does not carry -- carrying:

```
tier  health  model  provider  selection  slot  fallback_index
reason          concise summary; "primary unavailable; fallback #1 verified
                healthy" once probed, the plain routing reason offline
routing_reason  the router's own full derivation, always present and untouched
verified        did anything check at all

slots[]: model  provider  enabled  state  position
          probed         a request was made for this slot
          verified       a request was made AND the model was confirmed usable
          verify_code    machine-readable VerificationCode value, or ""
          verify_reason  bounded operator-safe sentence, or "" if not probed
          available      the availability fact on its own
```

Each slot reports `enabled` **and** `state` separately. That is deliberate: a
consumer given only a boolean has to re-derive the difference between "an
operator switched this off" and "this is currently unreachable", and would
eventually re-derive it wrongly.

`probed` and `verified` are separated for the same reason. Both false is a
different situation from either alone: a slot that was never asked about and a
slot that was asked about and rejected both read `verified: false`, and
`verify_reason` is what distinguishes them -- empty means nobody asked,
populated means somebody did and here is what happened.

`verify_code` exists so a UI never has to pattern-match `verify_reason` to tell
an auth failure from a timeout. The renderer writes the sentence; a consumer
binds the code.

`reason` and `routing_reason` are separate fields rather than one concatenated
string. A status line wants the conclusion; a routing audit wants the
derivation. Concatenating them produced the same fact twice in two vocabularies.

`forge models status --json` emits these objects newline-delimited, and the
Workbench indicator binds to the same shape rather than introducing a second
format. `GET /api/models` returns these entries verbatim. The renderer decides
nothing: it reads these fields and formats them, so presentation cannot change a
health value.

Health is defined once, in `ModelRegistry.health`. The router recomputes nothing
and `render.py` imports neither module -- presentation cannot change a health
value.

## The Workbench indicator consumes this, and decides nothing

The Workbench's model-health indicator is the first UI consumer of this contract,
and it is bound by the rule above in a way a test enforces rather than a comment
promises.

`forge/ui/static/health.js` maps the backend's `health` string to a glyph and a
word and does nothing else. It has no `GREEN`/`YELLOW`/`RED` comparison, no
arithmetic over slot states, and no model id or provider name anywhere in the
file. Two structural tests in `tests/test_ui_model_health_ui.py` hold that: one
fails if the frontend compares a health value or assigns one, the other fails if
a provider or tier name appears in the source.

Three consequences the indicator is built around:

**Unverified is its own state, not a GREEN.** An offline registry reports
configured-but-unchecked slots, and `ModelRegistry.health` calls that GREEN --
correctly, because "usable" means "configured and routable", not "checked". The
snapshot's own `verified` flag is therefore rendered as a distinct visual state
(`○`, "unverified") rather than folded into the health colour. Painting an
unchecked configuration as a healthy green dot would claim a check that never
happened, which is the same confusion `forge models status --verify`'s VERIFIED
column exists to prevent. An unknown health string also renders as its own
"unknown" state rather than defaulting to a colour.

**A failed optional slot does not darken its tier.** A dead fallback is a fact
about one slot, reported in that tier's details with the provider's own
`verify_reason` and `verify_code`. The tier indicator keeps showing the health
value the registry computed. A frontend that recomposed a tier's health from its
slots would be a second opinion about routing, and two opinions about whether a
model can serve are worse than one.

**Operational facts read as operational.** A stopped local llama.cpp reports
`verify_code: unreachable` with the transport's own reason, presented as a
property of that provider. It is not dressed as a Forge application failure,
because it is not one and an operator's next action -- start the service --
differs completely.

Accessibility is a property of the indicator, not a garnish: every tier carries a
per-state glyph, a visible word, and an `aria-label` naming the tier, the
backend's health word, and its plain-language meaning. Colour is never the only
signal.

## Commands

```
forge models status [--config PATH] [--json] [--verify]
forge route --agent NAME --task KIND --difficulty LEVEL [--tier NAME] [--json]
```

`forge route` and plain `forge models status` are inspection commands: no
gateway is built, no request is made, no credential is read. `forge route`
reports the tier, model, provider, whether primary or fallback, the reason, and
the routing rule matched.

`forge models status --verify` is the one exception, and it is opt-in. It
builds verifiers (not gateways), makes one bounded request per distinct model,
and reads the credential that request needs. It changes **no** routing semantics:
health comes from the same `TierConfig.health()` call the offline path uses.

The offline path is a separate function from the verified one
(`_run_models_status_offline` / `_run_models_status_verified`), so "plain status
performs zero network activity" is a structural property of a small function
rather than a promise about a larger one. Two tests assert it: one patches
`socket` to fail on any connection during `forge models status`, the other
scans the offline handler for any probe import or credential reference.

`--verify` also prints the per-provider **verification summary**, described above.
It is human-facing only and is deliberately absent from `--json`, whose
newline-delimited tier objects are a machine contract a summary line would
corrupt.

Exit codes are shared by both paths: `EXIT_OK` unless some tier is RED, in which
case `EXIT_MODEL_UNAVAILABLE`. YELLOW does not affect the exit code -- traffic
still flows on a degraded tier. A stopped local endpoint produces RED, not a
special-cased pass.

`forge route` and `forge models status` require **no workspace**. Routing is not
an operation on a workspace, and the workspace root being an explicit safety
decision should not be attached to an unrelated concern.

## Overrides bypass routing

Two overrides take precedence over the registry, and both **skip routing
entirely**:

| Source | Precedence |
|---|---|
| `--model ID` on `forge byte` / `forge neighbor` / `forge workbench` | highest |
| `$FORGE_MODEL` | second |
| the registry, by routing | default |

An override short-circuits before the registry is read. The registry is not
consulted, not validated, and cannot fail the run -- so **pinning a model keeps
working when the configuration file is broken**. That is deliberate: the escape
hatch has to stay usable precisely when it is needed.

An overridden model keeps the pre-existing provider behaviour, taking its
provider from `$FORGE_MODEL_PROVIDER`. Only a *routed* default takes its
provider from the selected slot, because that is the pairing configuration
stated; mixing the two would address a model to the wrong endpoint.

`forge byte` routes as task `coding`, difficulty `standard`; `forge neighbor`
routes as `conversation`, `standard`. Both pairs are named in
`forge/cli/main.py` and neither is an agent concern -- `AgentProfile.model`
remains `None` by settled decision.

### The four things that are not the same

Manual model selection is easy to conflate with three neighbouring mechanisms,
so the distinction is stated rather than left to be inferred from behaviour.

| | What decides it | Lifetime | Shape |
|---|---|---|---|
| **Agent identity** | The contract, per agent | permanent | `AgentProfile`: name, instructions, allowed tools |
| **Routing default** | The registry, from task + difficulty | per resolution | `[routing.tasks.*]` in `models.toml` |
| **Invocation override** | A human, per run | one invocation | `--model ID` or `$FORGE_MODEL` |
| **Health verification** | The provider, on request | a snapshot | `forge models status --verify`, `Availability` |

They are orthogonal. An override says *this run, this model* and nothing else;
it does not change what the next run routes to, what the agent is, or what the
registry believes. Verification is not a fourth selection mechanism -- it
supplies facts about reachability and never chooses a model.

### What an override is not

**Not an `AgentProfile` field.** `AgentProfile.model` stays `None` for every
agent, and there is no `override`, `preferred_model`, or `pinned_model` field.
An override that could be recorded on a profile would become a persistent
per-agent preference, and the routing layer would stop being the single source
of the default. `tests/test_model_override.py` asserts the field set exactly.

**Not a registry mutation.** An override changes nothing in `models.toml`, in
the registry object, or in any tier. `tests/test_model_override.py` runs a full
pinned run and compares the workspace tree across it.

**Not a trust signal.** Selecting a model does not assert that it works. Forge
does not treat an override as "use this anyway"; it uses it, and the provider
decides whether it exists. That is the honest relationship -- Forge cannot know
the provider's catalogue, and an operator pinning a model they know about is
adding information Forge lacks.

### The advisory, and why it cannot block

An override is **reported on, never validated against.** Where the registry is
readable, Forge checks whether the selected model is one it knows about and
prints a warning on stderr:

| Registry says | Advisory |
|---|---|
| not configured at all | `'vendor/typo' is not configured in the model registry (path); the run will use it anyway...` |
| configured, `enabled = false` | `'vendor/off' is configured but disabled (enabled = false); the run will attempt it anyway` |
| configured, enabled, provider says unusable | `'vendor/x' is configured but its provider reports it unusable right now; ...` |
| configured and usable | *silence* |
| **could not be read** | *silence* |

**The warning never changes the outcome.** The resolved model is the selected
one either way, and the run proceeds. Two reasons, and the second is the
important one:

1. Turning a warning into a refusal would remove the escape hatch at the moment
   it is needed -- an operator reaching for `--model` is usually doing so
   *because* the configuration is broken.
2. A warning that can fail a run is a second policy, and the whole point of the
   override is that it is not one.

Silence when the registry cannot be read is deliberate for the same reason: an
operator bypassing a broken file should not also be handed a complaint about it.

Reachability is reported only when something actually checked. With no
`Availability` source the registry knows nothing about whether a model answers,
and claiming one is unavailable because nobody asked would be inventing a
finding.

### No silent fallback for an explicit selection

An override resolves to exactly the model named. It is never rewritten to a
tier's fallback, because no tier is consulted -- there is nothing to fall back
*from*. An operator told they ran `vendor/x` has run `vendor/x`, or has had a
provider error, and never something else.

This is the opposite of the *routed* path, where falling back is the whole
point: a tier declares an ordered list of slots precisely so traffic keeps
flowing when the primary is down.

The asymmetry is the point, and it is drawn at the routing layer rather than in
the CLI:

| | Override | Routed |
|---|---|---|
| Registry consulted | no | yes |
| Broken config | runs anyway | fails, naming the file |
| Nothing usable | provider error at first call | `no usable model for <agent>'s default routing...` |
| Falls back | never | yes, per the tier's `fallbacks` |

### Both agents, one surface

`forge byte` and `forge neighbor` take an identical `--model ID`, resolved
identically. **Agent identity and manual model selection are independent axes:**
an override selects a model without changing which agent runs, and the two
agents' routing defaults (`coding` vs `conversation`) are unaffected by it.
Nothing about a run is remembered afterwards, so there is no way for an override
to become an agent's standing preference.

> **`$FORGE_MODEL` shadows the registry silently.** If it is exported in your
> shell, `forge byte` will keep using it and registry edits will appear to be
> ignored -- including a disabled primary or a RED tier. It now emits the same
> advisory as `--model` when the model is one the registry does not carry, but
> a model that *is* configured will pass unnoticed. Run `unset FORGE_MODEL`
> before testing registry behaviour, or check `forge route` output, which is
> unaffected by the override.

## Still not connected

`forge byte` and `forge workbench` resolve their **default** model through the
registry. Nothing about execution changed: approvals, tool dispatch, the tool
loop, and the workspace rules are untouched, and `AgentRuntime` still receives a
plain `model_id` string.

What is deliberately still not done: the registry is resolved **once, at the CLI
edge**. It is not threaded into the runtime, and no model call is retried on a
different model mid-run. A run that starts on a fallback keeps that model;
switching mid-run is a separate decision. A workbench snapshot going stale is
likewise not corrected mid-run -- the indicator shows the last checked answer and
says when it was checked, and correcting it is the operator's explicit act.

### Health is checked at startup, and on request only

`forge workbench` performs **one** verified health pass before it serves, so the
first page load shows a checked answer rather than asking the operator whether
the indicator has been checked yet. One pass, at startup. Not on a timer.

From there, the operator drives every subsequent check:

| Route | Method | What it does |
|---|---|---|
| `/api/models` | GET | Returns the cached snapshot. **Never probes.** |
| `/api/models/verify` | POST | One bounded verification pass, then the new snapshot. |

`GET /api/models` is strictly read-only. It returns
`ModelRouter.status()` entries verbatim -- no renamed fields, no derived
booleans, no filtering -- so it cannot disagree with `forge models status` about
what a tier's health is. A page load, a refresh, or an operator hammering F5
cannot spend a provider request or present a credential, and the test asserts a
probe call count rather than trusting the intent.

`POST /api/models/verify` is the only way to make the server contact a provider
after startup. It is a command, not an observation, so it takes the same
exact-Origin and token guards as every other state-changing route on that
surface, and it **refuses while a run is in flight** (409). That refusal is the
point: a verification pass opening sockets from inside a process that is already
executing would be an incidental side effect of running Byte, and health
checking must never be one. Not queued either -- the operator asked a question,
and an answer from after the run is a different answer.

Its bounds are the verifier layer's own and nothing else: one attempt per
distinct `provider/model`, no retries, five seconds per attempt. `ModelHealth`
holds no timeout constant, so a more generous one cannot appear for the
indicator without editing the single place that defines the policy.

### No polling, and no incidental verification

There is no timer anywhere in this path. The page reads the cache on load and
when the tab becomes visible again, and `POST`s only when the operator presses
Re-check. A backgrounded tab that re-checked on focus would turn a refocus into a
repeating credential-bearing request.

A configuration that will not load does not stop the workbench from serving. An
operator who passed `--model` over a broken registry still gets a working
surface, and `/api/models` answers 503 with the registry's own error -- which
names a table and a rule and carries no file contents. An empty indicator would
read as "nothing configured and nothing wrong", which is a different statement
from "Forge could not read your configuration".

Not changed by any of this: `forge byte`, the agent runtime, tool dispatch,
approvals, the workspace rules, command classification, and routing policy. A RED
tier still fails to start a run, exactly as before; the indicator tells you
*before* you start what would have happened.