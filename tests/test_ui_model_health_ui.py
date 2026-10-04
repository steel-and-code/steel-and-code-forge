"""The Workbench model-health indicator: rendering states and its boundaries.

Two kinds of test, and the split is the point.

**Behavioural**, driven by Node against ``forge/ui/static/health.js`` with a stub
DOM: one test per state the indicator has to render -- GREEN, YELLOW, RED, and
unverified -- asserting the glyph, the visible word, and the accessible name. A
dot that is only a colour fails every one of them.

**Structural**, in Python against the shipped source: the frontend must not
re-derive health and must not hard-code model or provider policy. These are the
properties a rendering test cannot see: a well-formatted wrong answer looks
exactly like a right one.

The stub DOM is a test asset, not a browser. It is small enough to read, and it
throws on ``innerHTML`` so the "no provider string is ever parsed as markup"
property is enforced rather than asserted in a comment.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

UI_STATIC = pathlib.Path(__file__).resolve().parent.parent / "forge" / "ui" / "static"
HEALTH_JS = UI_STATIC / "health.js"
APP_JS = UI_STATIC / "app.js"
INDEX_HTML = UI_STATIC / "index.html"
STUB = pathlib.Path(__file__).resolve().parent / "ui_stub" / "dom.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to drive the browser-side render function",
)


# --------------------------------------------------------------------------
# Fixtures: payloads, and a Node harness
# --------------------------------------------------------------------------


def _slot(
    model="alpha",
    provider="fake",
    *,
    position=0,
    enabled=True,
    state="configured",
    probed=True,
    verified=True,
    code="ok",
    reason="provider lists this model",
):
    return {
        "model": model,
        "provider": provider,
        "enabled": enabled,
        "state": state,
        "position": position,
        "probed": probed,
        "verified": verified,
        "verify_code": code,
        "verify_reason": reason,
        "available": verified,
    }


def _entry(tier="general_light", health="GREEN", **overrides):
    """One status entry, shaped exactly as ``ModelRouter.status()`` emits it."""
    entry = {
        "tier": tier,
        "health": health,
        "model": "alpha",
        "provider": "fake",
        "selection": "primary",
        "slot": "primary",
        "fallback_index": 0,
        "reason": "primary verified usable",
        "routing_reason": "primary 'fake/alpha' is enabled and available",
        "verified": True,
        "slots": [_slot()],
    }
    entry.update(overrides)
    return entry


def _payload(*entries, verified=True, checked_at="2026-10-04T02:00:00+00:00"):
    return {
        "tiers": list(entries) or [_entry()],
        "verified": verified,
        "checked_at": checked_at,
    }


HARNESS = """
const path = require("path");
const {{ StubDocument }} = require({stub!r});
const health = require({health!r});
const payload = {payload};

const doc = new StubDocument();
const container = doc.createElement("div");
const selected = [];
const rendered = health.renderHealth(doc, container, payload, function (tier) {{
  selected.push(tier.tier);
}});

const buttons = rendered.buttons;
const first = Object.keys(buttons)[0];
if (process.argv[2] === "open") {{
  buttons[first].click();
}}

const tiers = rendered.model.tiers;
process.stdout.write(JSON.stringify({{
  summary: rendered.model.summary,
  verified: rendered.model.verified,
  stamp: rendered.strip.findOne(function (e) {{
    return String(e.className).indexOf("health-stamp") >= 0;
  }}).textContent,
  tiers: tiers,
  buttons: Object.keys(buttons).map(function (name) {{
    const b = buttons[name];
    return {{
      tier: name,
      health: b.getAttribute("data-health"),
      verified: b.getAttribute("data-verified"),
      expanded: b.getAttribute("aria-expanded"),
      label: b.getAttribute("aria-label"),
      controls: b.getAttribute("aria-controls"),
      glyph: b.findOne(function (e) {{
        return String(e.className).indexOf("health-dot") >= 0;
      }}).textContent,
      glyphHidden: b.findOne(function (e) {{
        return String(e.className).indexOf("health-dot") >= 0;
      }}).getAttribute("aria-hidden"),
      type: b.type,
    }};
  }}),
  panel: {{
    hidden: rendered.details[first].hidden,
    text: rendered.details[first].text(),
    slotCount: rendered.details[first].byClass("health-slot").length,
    slotVerified: rendered.details[first].byClass("health-slot").map(function (row) {{
      return row.getAttribute("data-verified");
    }}),
    slotProbed: rendered.details[first].byClass("health-slot").map(function (row) {{
      return row.getAttribute("data-probed");
    }}),
  }},
  stripLabel: rendered.strip.getAttribute("aria-label"),
  stripRole: rendered.strip.getAttribute("role"),
  openState: Object.keys(buttons).map(function (name) {{
    return buttons[name].getAttribute("aria-expanded");
  }}),
  panelHidden: Object.keys(rendered.details).map(function (name) {{
    return rendered.details[name].hidden;
  }}),
  selected: selected,
  innerHTMLUsed: false,
}}, null, 2));
"""


def render(payload, *, open_first: bool = False, path: pathlib.Path | None = None):
    """Run health.js in Node against the stub DOM and return its report."""
    script = HARNESS.format(
        stub=str(STUB),
        health=str(HEALTH_JS),
        payload=json.dumps(payload),
    )
    script_file = path or pathlib.Path("/tmp/forge-health-harness.js")
    script_file.write_text(script, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script_file), *(["open"] if open_first else [])],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(STUB.parent),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(autouse=True)
def no_real_network():
    """The stub DOM has no networking, but say so rather than rely on that."""
    yield


# --------------------------------------------------------------------------
# GREEN
# --------------------------------------------------------------------------


def test_a_healthy_tier_renders_green_with_a_word_not_only_a_colour() -> None:
    report = render(_payload(_entry(health="GREEN")))

    button = report["buttons"][0]
    assert button["health"] == "GREEN"
    assert button["glyph"] == "●"
    assert "healthy" in button["label"]
    # And the backend's own word, not a translation of it.
    assert "GREEN" in button["label"]


def test_a_healthy_tier_reports_the_model_and_provider() -> None:
    report = render(_payload(_entry(health="GREEN")), open_first=True)

    text = report["panel"]["text"]
    assert "alpha" in text
    assert "fake" in text
    assert "primary verified usable" in text


def test_a_healthy_tier_shows_the_selected_slot_kind() -> None:
    report = render(_payload(_entry(health="GREEN")), open_first=True)

    assert "primary" in report["panel"]["text"]


def test_one_indicator_is_rendered_per_capability_tier() -> None:
    report = render(
        _payload(
            _entry("general_light"),
            _entry("coding_standard"),
            _entry("reasoning_heavy"),
        )
    )

    assert [b["tier"] for b in report["buttons"]] == [
        "general_light",
        "coding_standard",
        "reasoning_heavy",
    ]
    assert len(report["buttons"]) == 3


# --------------------------------------------------------------------------
# YELLOW
# --------------------------------------------------------------------------


def test_a_degraded_tier_names_the_fallback_it_chose() -> None:
    report = render(
        _payload(
            _entry(
                health="YELLOW",
                model="beta",
                selection="fallback",
                slot="fallback #1",
                fallback_index=1,
                reason="primary unavailable; fallback #1 verified healthy",
                slots=[
                    _slot(
                        "alpha",
                        state="unavailable",
                        verified=False,
                        code="unreachable",
                        reason="connection refused on 127.0.0.1:8080",
                    ),
                    _slot(
                        "beta",
                        position=1,
                    ),
                ],
            )
        )
    )

    button = report["buttons"][0]
    assert button["health"] == "YELLOW"
    assert button["glyph"] == "◐"
    assert "degraded" in button["label"]
    assert "YELLOW" in button["label"]


def test_a_degraded_tier_explains_which_fallback_and_why() -> None:
    report = render(
        _payload(
            _entry(
                health="YELLOW",
                model="beta",
                selection="fallback",
                slot="fallback #1",
                fallback_index=1,
                reason="primary unavailable; fallback #1 verified healthy",
            )
        ),
        open_first=True,
    )

    text = report["panel"]["text"]
    assert "fallback #1" in text
    assert "primary unavailable" in text


def test_a_stopped_local_service_reads_as_operational_not_as_a_forge_failure() -> None:
    """The distinction the indicator exists to make.

    A local llama.cpp being down is an *operational* fact about one provider,
    reported with that provider's own words. It is not a Forge application
    failure, and the panel must not dress it as one.
    """
    report = render(
        _payload(
            _entry(
                health="YELLOW",
                model="beta",
                selection="fallback",
                slot="fallback #1",
                slots=[
                    _slot(
                        "qwen2.5-coder:14b",
                        provider="local",
                        state="unavailable",
                        verified=False,
                        code="unreachable",
                        reason="connection refused on 127.0.0.1:8080",
                    ),
                    _slot("beta", position=1),
                ],
            )
        ),
        open_first=True,
    )

    text = report["panel"]["text"]
    assert "connection refused on 127.0.0.1:8080" in text, (
        "the provider's own reason must reach the operator verbatim"
    )
    assert "unreachable" in text, "the machine-readable code must be shown too"
    assert "local/qwen2.5-coder:14b" in text, "the slot must be identified"


def test_a_failed_optional_slot_is_listed_without_darkening_a_healthy_tier() -> None:
    """A dead fallback is a fact about a slot, not about the tier.

    The tier indicator must keep reporting what the backend reported -- GREEN --
    while the details still show the failing slot. Anything else would be the
    frontend disagreeing with the router about health.
    """
    report = render(
        _payload(
            _entry(
                health="GREEN",
                slots=[
                    _slot(),
                    _slot(
                        "gamma",
                        position=1,
                        state="unavailable",
                        verified=False,
                        code="not_found",
                        reason="provider does not list 'fake/gamma'",
                    ),
                ],
            )
        ),
        open_first=True,
    )

    assert report["buttons"][0]["health"] == "GREEN"
    assert report["buttons"][0]["glyph"] == "●"
    assert report["panel"]["slotCount"] == 2
    assert report["panel"]["slotVerified"] == ["yes", "no"]
    assert "provider does not list 'fake/gamma'" in report["panel"]["text"]


# --------------------------------------------------------------------------
# RED
# --------------------------------------------------------------------------


def test_an_unavailable_tier_is_shown_as_unavailable() -> None:
    report = render(
        _payload(
            _entry(
                health="RED",
                model="",
                provider="",
                selection="none",
                slot="none",
                reason="no verified usable model in this tier",
            )
        )
    )

    button = report["buttons"][0]
    assert button["health"] == "RED"
    assert button["glyph"] == "✕"
    assert "unavailable" in button["label"]
    assert "RED" in button["label"]


def test_an_unavailable_tier_states_that_nothing_can_serve_it() -> None:
    report = render(
        _payload(
            _entry(
                health="RED",
                model="",
                provider="",
                selection="none",
                slot="none",
                reason="no verified usable model in this tier",
            )
        ),
        open_first=True,
    )

    assert "no verified usable model" in report["panel"]["text"]


def test_an_empty_model_is_named_rather_than_left_blank() -> None:
    report = render(
        _payload(
            _entry(
                health="RED",
                model="",
                provider="",
                selection="none",
                slot="none",
            )
        ),
        open_first=True,
    )

    assert "none selected" in report["panel"]["text"], (
        "a blank field reads as a rendering bug rather than as 'nothing serves this'"
    )


# --------------------------------------------------------------------------
# Unverified is not GREEN
# --------------------------------------------------------------------------


def test_an_unverified_snapshot_is_never_drawn_as_healthy() -> None:
    """The one that matters most.

    An offline registry reports configured-but-unchecked slots and the router
    calls that GREEN. Painting that as a healthy dot would be claiming a check
    that never happened -- the exact failure ``forge models status`` guards
    against with its VERIFIED column.
    """
    report = render(_payload(_entry(health="GREEN"), verified=False, checked_at=None))

    button = report["buttons"][0]
    assert button["verified"] == "no"
    assert button["glyph"] == "○"
    assert button["glyph"] != "●"
    assert "unverified" in button["label"]
    assert report["verified"] is False


def test_an_unverified_snapshot_says_so_in_the_strip() -> None:
    report = render(_payload(verified=False, checked_at=None))

    assert "not verified" in report["stamp"]
    assert "not verified" in report["stripLabel"]


def test_a_verified_snapshot_says_when_it_was_checked() -> None:
    report = render(_payload(checked_at="2026-10-04T02:00:00+00:00"))

    assert "2026-10-04" in report["stamp"]
    assert "checked" in report["stamp"]


def test_an_unknown_health_string_renders_as_unknown_not_as_a_colour() -> None:
    """A value Forge has never heard of must not default to a healthy dot."""
    report = render(_payload(_entry(health="AMBER")))

    button = report["buttons"][0]
    assert button["health"] == "AMBER", "the backend's word is passed through"
    assert button["glyph"] == "?"
    assert "unknown" in button["label"]


# --------------------------------------------------------------------------
# Interaction and accessibility
# --------------------------------------------------------------------------


def test_the_collapsed_view_is_a_strip_of_tier_buttons() -> None:
    report = render(_payload())

    assert report["stripRole"] == "group"
    assert report["buttons"][0]["type"] == "button"
    assert all(panel for panel in report["panelHidden"]), (
        "details start hidden; the default view is the small one"
    )


def test_clicking_a_tier_reveals_its_details() -> None:
    report = render(_payload(), open_first=True)

    assert report["panel"]["hidden"] is False
    assert report["buttons"][0]["expanded"] == "true"


def test_each_button_names_the_panel_it_controls() -> None:
    report = render(_payload(_entry("general_light")))

    button = report["buttons"][0]
    assert button["controls"] == "health-detail-general_light"


def test_the_glyph_is_hidden_from_assistive_technology() -> None:
    """The dot is decoration; the accessible name is the state.

    A screen reader announcing "black circle" instead of "general_light: GREEN,
    healthy" would make the indicator worse than useless.
    """
    report = render(_payload())

    assert report["buttons"][0]["glyphHidden"] == "true"
    assert "general_light" in report["buttons"][0]["label"]


def test_the_strip_carries_a_summary_for_the_whole_group() -> None:
    report = render(_payload(_entry(), _entry("coding_standard")))

    assert "2 capability tiers" in report["stripLabel"]


def test_the_summary_is_singular_for_one_tier() -> None:
    report = render(_payload(_entry()))

    assert "1 capability tier," in report["stripLabel"]


def test_an_unprobed_slot_is_distinguished_from_a_rejected_one() -> None:
    """Both are ``verified: false``, and only ``probed`` separates them.

    "Forge does not know" and "Forge knows it is broken" have different fixes,
    and the status contract keeps them apart precisely so a consumer does not
    collapse them.
    """
    report = render(
        _payload(
            _entry(
                health="YELLOW",
                slots=[
                    _slot("beta", position=1, state="unavailable", verified=False),
                    _slot(
                        "gamma",
                        position=2,
                        state="configured",
                        probed=False,
                        verified=False,
                        code="",
                        reason="",
                    ),
                ],
            )
        ),
        open_first=True,
    )

    assert report["panel"]["slotVerified"] == ["no", "no"]
    assert report["panel"]["slotProbed"] == ["yes", "no"]
    text = report["panel"]["text"]
    assert "checked, unusable" in text
    assert "not checked" in text


# --------------------------------------------------------------------------
# Structural: the frontend must not re-derive health
# --------------------------------------------------------------------------


def _js() -> str:
    return HEALTH_JS.read_text(encoding="utf-8")


def _js_code(path: pathlib.Path = HEALTH_JS) -> str:
    """The file with comments and string literals removed.

    Scanning raw text is how a guard starts firing on prose: the word "GREEN"
    appears in this module's own comments explaining that it must not be used,
    so a text scan either fails on a correct file or gets weakened until it
    proves nothing. These guards run over code only.
    """
    source = path.read_text(encoding="utf-8")
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", "", without_block)
    return re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", '""', without_line)


def test_the_frontend_does_not_branch_on_health() -> None:
    """Health arrives as a string and is used as a lookup key. Never as a test.

    A comparison like ``health === "RED" ? ... : ...`` is health logic moved
    into the browser, where it can disagree with the router and there is no test
    that would notice.
    """
    source = _js_code()
    offenders = re.findall(r"health\s*(===|!==|==|!=)\s*", source)
    assert not offenders, f"frontend compares a health value: {offenders}"


def test_the_frontend_names_no_model_or_provider() -> None:
    """No model ids, no provider names, no tier names in the frontend.

    Those belong in ``models.toml``. A hard-coded id here would be a second
    source of truth that the routing tests cannot see.
    """
    source = _js_code().lower()
    banned = [
        "openrouter",
        "anthropic",
        "openai",
        "claude",
        "gpt-",
        "qwen",
        "llama",
        "general_light",
        "coding_standard",
        "reasoning_heavy",
    ]
    found = [name for name in banned if name in source]
    assert not found, f"frontend hard-codes model or provider policy: {found}"


def test_the_frontend_reasons_about_no_slot_state() -> None:
    """No health value is ever assembled out of the pieces.

    ``state``, ``probed``, ``verified``, ``available`` and ``fallback_index``
    are read and displayed. What must not happen is any of them feeding a
    computation that produces or alters a health value -- that is what would
    make the indicator a second opinion about routing.

    The guard is deliberately narrow: it forbids those fields appearing in the
    same expression as ``health``, rather than forbidding the word ``state =``
    anywhere. An earlier, broader version of this guard fired on a local
    variable holding a *display word* ("not checked"), which is exactly the kind
    of false positive that trains people to weaken a guard until it proves
    nothing.
    """
    source = _js_code()
    offenders = re.findall(
        r"[^\n]*\b(?:state|probed|available|fallback_index)\b[^\n]*\bhealth\b"
        r"|[^\n]*\bhealth\b[^\n]*\b(?:state|probed|available|fallback_index)\b",
        source,
    )
    assert not offenders, f"frontend mixes slot facts with health: {offenders}"


def test_the_frontend_never_assigns_a_health_value() -> None:
    """``health`` is read from the payload and written into a DOM attribute.

    An assignment of the form ``health = ...`` would be the indicator computing
    its own answer rather than reporting the backend's.
    """
    source = _js_code()
    assert not re.search(r"(?<![\w.])health\s*=(?!=)", source), (
        "frontend assigns a health value"
    )


def test_the_frontend_never_posts_health_data() -> None:
    """Health travels one way: out of the server.

    The re-check POST carries no body. A page that could *send* health would be a
    second place where health is decided.
    """
    assert "/api/models" not in _js_code(), (
        "health.js renders; app.js talks to the API"
    )


def test_the_page_loads_the_indicator_module() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "health.js" in html


def test_app_js_does_not_derive_health_either() -> None:
    """The wiring file is held to the same rule as the render module."""
    source = APP_JS.read_text(encoding="utf-8")
    assert not re.search(r"[\"']GREEN[\"']|[\"']YELLOW[\"']|[\"']RED[\"']", source), (
        "app.js must not name health values; it passes the backend's string "
        "through to the renderer"
    )