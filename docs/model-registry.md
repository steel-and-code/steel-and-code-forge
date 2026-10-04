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
    providers.py   ProviderSpec, ProviderCatalog -- names, not classes
    registry.py    ModelSlot, TierConfig, RoutingTable, Availability,
                   ModelRegistry -- configuration and health
    router.py      RouteRequest, RouteDecision, ModelRouter -- selection
    models.toml    the packaged default registry
```

Dependencies run one way: `router -> registry -> providers -> tiers`. Nothing in
this chain imports a gateway, imports `forge.tools`, or opens a socket. Deciding
is not calling.

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

## Configured, enabled, available

Three separate facts. A health display that collapses them into one boolean
lies, and the distinctions have different fixes:

| Fact | Meaning | Set by |
|---|---|---|
| **configured** | The slot exists in the configuration file. | Editing the file. |
| **enabled** | `enabled = true`. | An operator, by decision. |
| **available** | An availability source says it could serve now. | An injected source. |

`SlotState` records the distinction without hiding it:

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
             gateway_factory=...)   # or None
```

`forge.models.registry` and `forge.models.router` import no gateway. Providers
are written down in exactly one place, `ProviderCatalog.default()`.

| Provider | Routable | Credential |
|---|---|---|
| `openrouter` | yes | `FORGE_OPENROUTER_API_KEY` |
| `local` | yes | none |
| `openai` | no | `FORGE_OPENAI_API_KEY` |
| `anthropic` | no | `FORGE_ANTHROPIC_API_KEY` |

The asymmetry is deliberate. Declaring `openai` and `anthropic` means a
configuration naming them **validates** and `forge models status` can show the
slot honestly, rather than it being silently dropped. Leaving them without a
gateway means routing to one returns a structured failure naming the provider --
never an import error inside a run. An unimplemented provider is *declared but
unroutable*, which is why such a tier is RED rather than reassuring.

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

`Availability` is the injection point. It accepts a mapping or a callable and
performs **no network I/O**:

```python
Availability(source={"local/qwen2.5-coder:14b": False})   # by provider/model
Availability(source=lambda slot: ...)                      # for a future probe
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

A future probe drops into this same seam through
`registry.with_availability(...)`; the registry is immutable, so it returns a new
value rather than mutating the one a caller holds. **A probe module imports this
one; this one never imports a probe.** The decision path must not be able to make
a network call.

Credentials are deliberately *not* consulted for health. A missing key is
configuration, not evidence a model is unreachable.

## UI consumers must use `ModelRouter.status()`

> `ModelRouter.status()` is the **source of truth** for model health in any
> surface. A UI health indicator consumes it verbatim. It must not recompute
> health, re-read the registry, or reformat the fields.

`status()` returns one entry per capability tier -- every known tier name,
including one the registry does not carry -- carrying:

```
tier  health  model  provider  selection  slot  reason  verified
slots[]: model  provider  enabled  state  position
```

Each slot reports `enabled` **and** `state` separately. That is deliberate: a
consumer given only a boolean has to re-derive the difference between "an
operator switched this off" and "this is currently unreachable", and would
eventually re-derive it wrongly. `forge models status --json` emits these
objects newline-delimited, and the Workbench indicator should bind to the same
shape rather than introducing a second format.

Health is defined once, in `ModelRegistry.health`. The router recomputes nothing
and `render.py` imports neither module -- presentation cannot change a health
value.

## Commands

```
forge models status [--config PATH] [--json]
forge route --agent NAME --task KIND --difficulty LEVEL [--tier NAME] [--json]
```

Both are inspection commands: no gateway is built, no request is made, no
credential is read. `forge route` reports the tier, model, provider, whether
primary or fallback, the reason, and the routing rule matched.

`forge route` and `forge models status` require **no workspace**. Routing is not
an operation on a workspace, and the workspace root being an explicit safety
decision should not be attached to an unrelated concern.

## Overrides bypass routing

Two overrides take precedence over the registry, and both **skip routing
entirely**:

| Source | Precedence |
|---|---|
| `--model ID` on `forge byte` / `forge workbench` | highest |
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

`forge byte` routes as task `coding`, difficulty `standard`. Both are named in
`forge/cli/main.py` and neither is an agent concern -- `AgentProfile.model`
remains `None` by settled decision.

> **`$FORGE_MODEL` shadows the registry silently.** If it is exported in your
> shell, `forge byte` will keep using it and registry edits will appear to be
> ignored -- including a disabled primary or a RED tier. Run
> `unset FORGE_MODEL` before testing registry behaviour, or check
> `forge route` output, which is unaffected by the override.

## Not connected yet

`forge byte` and `forge workbench` resolve their **default** model through the
registry. Nothing else about execution changed: approvals, tool dispatch, the
tool loop, and the workspace rules are untouched, and `AgentRuntime` still
receives a plain `model_id` string.

What is deliberately not done: the registry is resolved **once, at the CLI
edge**. It is not threaded into the runtime, no health probing happens, and no
model call is retried on a different model mid-run. A run that starts on a
fallback keeps that model; switching mid-run is a separate decision.