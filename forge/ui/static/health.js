/* Forge Workbench — the model-health indicator.
 *
 * This file exists apart from app.js for one reason: its decisions are testable
 * without a browser. Everything here is a pure function over the status payload
 * `GET /api/models` returns, plus one renderer that takes a `document`. The Node
 * test in `tests/test_ui_model_health_ui.py` drives both against a stub DOM.
 *
 * Three rules this file keeps, and each is load-bearing:
 *
 *  - IT NEVER DECIDES HEALTH. `entry.health` arrives as "GREEN", "YELLOW", or
 *    "RED" from `ModelRouter.status()` and is used as a lookup key for
 *    presentation. There is no arithmetic over slot states, no fallback chain,
 *    and no "is the primary usable" reasoning here. A frontend that re-derived
 *    health would be a second opinion about routing, and two opinions about
 *    whether a model can serve are worse than one. `tests/test_ui_model_health_ui.py`
 *    asserts this structurally: an unknown health string renders as an explicit
 *    "unknown" rather than defaulting to a colour.
 *
 *  - COLOUR IS NEVER THE ONLY SIGNAL. Every indicator carries a glyph that
 *    differs per state, a visible word, and an accessible name. A red/green dot
 *    alone is unreadable to a colour-blind operator and invisible to a screen
 *    reader, which is exactly when a RED tier most needs to be noticed.
 *
 *  - UNVERIFIED IS ITS OWN STATE, NOT A GREEN. An offline registry reports
 *    "configured" slots, and the router reports GREEN for those. Showing that
 *    as a healthy green dot would be claiming a check that never happened, so
 *    an unverified snapshot renders hollow with the word "unverified".
 *
 * The payload is used verbatim. `model`, `provider`, `selection`, `reason`,
 * `routing_reason`, `verified`, and each slot's `state` / `probed` / `verified`
 * / `verify_reason` / `verify_code` are displayed as the backend wrote them.
 * Nothing is renamed, reformatted, or inferred.
 */

(function (root, factory) {
  "use strict";

  var api = factory();

  // Two consumers, one file: the page reads it off `window`, and the Node test
  // requires it as a module. No build step, no bundler, no framework.
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ForgeHealth = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Presentation only. The keys are the backend's health vocabulary and the
  // values are what a person reads; nothing here feeds a decision back.
  var HEALTH_PRESENTATION = {
    GREEN: { word: "healthy", glyph: "●" },
    YELLOW: { word: "degraded", glyph: "◐" },
    RED: { word: "unavailable", glyph: "✕" },
  };

  // A health string Forge has never heard of. Rendered as its own visible state
  // rather than falling through to a colour, because silently painting an
  // unknown value green is the one failure this file must not have.
  var UNKNOWN_HEALTH = { word: "unknown", glyph: "?" };

  // Shown when the snapshot came from configuration alone.
  var UNVERIFIED = { word: "unverified", glyph: "○" };

  function presentationFor(health, verified) {
    if (!verified) return UNVERIFIED;
    return HEALTH_PRESENTATION[health] || UNKNOWN_HEALTH;
  }

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return fallback === undefined ? "not reported" : fallback;
    }
    return String(value);
  }

  function slotLabel(slot) {
    var position = Number(slot.position) || 0;
    return position === 0 ? "primary" : "fallback #" + position;
  }

  /* What Forge knows about one slot, in the backend's words.
   *
   * `verified` is reported as the backend states it and never recomputed: a slot
   * nobody probed and a slot that was probed and rejected are both
   * `verified: false`, and `probed` is what separates them. Collapsing those two
   * here would destroy the exact distinction the status contract exists to
   * preserve — "Forge does not know" and "Forge knows it is broken" have
   * different fixes.
   */
  function slotModel(slot) {
    var probed = Boolean(slot.probed);
    var verified = Boolean(slot.verified);

    var state;
    if (!probed) state = "not checked";
    else if (verified) state = "checked, usable";
    else state = "checked, unusable";

    return {
      label: slotLabel(slot),
      model: text(slot.model, "none"),
      provider: text(slot.provider, "none"),
      state: text(slot.state),
      enabled: Boolean(slot.enabled),
      probed: probed,
      verified: verified,
      verifyCode: text(slot.verify_code, ""),
      verifyReason: text(slot.verify_reason, ""),
      summary: state,
    };
  }

  /* One capability tier, as the page draws it.
   *
   * `slot` is the backend's `slot` field verbatim — "primary", "fallback #2", or
   * "none" — because that string already encodes which kind of slot was chosen
   * and its position. The frontend does not recompute it from `selection` and
   * `fallback_index`; it is told.
   */
  function tierModel(entry, snapshotVerified) {
    var presentation = presentationFor(entry.health, snapshotVerified);

    var accessible =
      entry.tier + ": " + text(entry.health) + ", " + presentation.word;

    return {
      tier: text(entry.tier),
      health: text(entry.health),
      healthWord: presentation.word,
      glyph: presentation.glyph,
      verified: Boolean(snapshotVerified),
      model: text(entry.model, "none selected"),
      provider: text(entry.provider, "none"),
      slot: text(entry.slot),
      selection: text(entry.selection),
      reason: text(entry.reason),
      routingReason: text(entry.routing_reason, ""),
      checked: Boolean(entry.verified),
      accessibleName: accessible,
      slots: (entry.slots || []).map(slotModel),
    };
  }

  /* The whole indicator: a snapshot plus one model per tier.
   *
   * `checkedAt` is rendered so an operator can tell a fresh answer from one
   * captured before a provider went down, which is the difference between "my
   * local service is off" and "Forge last looked an hour ago".
   */
  function healthModel(payload) {
    var body = payload || {};
    var verified = Boolean(body.verified);
    var tiers = (body.tiers || []).map(function (entry) {
      return tierModel(entry, verified);
    });

    return {
      tiers: tiers,
      verified: verified,
      checkedAt: body.checked_at || "",
      // One word for the whole strip, read by assistive tech before the tiers.
      summary: verified
        ? "Models: " +
          tiers.length +
          " capability " +
          (tiers.length === 1 ? "tier" : "tiers") +
          ", checked"
        : "Models: " +
          tiers.length +
          " capability " +
          (tiers.length === 1 ? "tier" : "tiers") +
          ", not verified",
    };
  }

  // -- rendering ---------------------------------------------------------
  //
  // `doc` is passed in rather than reached for globally, so this renderer runs
  // under a stub document in the test. Everything it emits is built with
  // createElement and textContent: no innerHTML, so a model id or a reason
  // string from a provider can never be parsed as markup.

  function node(doc, tag, className, textContent) {
    var element = doc.createElement(tag);
    if (className) element.className = className;
    if (textContent !== undefined && textContent !== null) {
      element.textContent = String(textContent);
    }
    return element;
  }

  function field(doc, label, value) {
    var wrapper = node(doc, "div", "health-field");
    var dt = node(doc, "dt", null, label);
    var dd = node(doc, "dd", "mono", text(value, "not reported"));
    wrapper.appendChild(dt);
    wrapper.appendChild(dd);
    return wrapper;
  }

  function slotRow(doc, slot) {
    var row = node(doc, "li", "health-slot");
    row.setAttribute("data-verified", slot.verified ? "yes" : "no");
    row.setAttribute("data-probed", slot.probed ? "yes" : "no");

    var head = node(
      doc,
      "p",
      "health-slot-head",
      slot.label + " · " + slot.provider + "/" + slot.model
    );
    row.appendChild(head);

    var facts = node(doc, "dl", "health-slot-facts");
    facts.appendChild(field(doc, "state", slot.state));
    facts.appendChild(field(doc, "enabled", slot.enabled ? "yes" : "no"));
    facts.appendChild(field(doc, "verification", slot.summary));
    if (slot.verifyCode) facts.appendChild(field(doc, "code", slot.verifyCode));
    facts.appendChild(field(doc, "reason", slot.verifyReason));
    row.appendChild(facts);

    return row;
  }

  function detail(doc, tier) {
    var body = node(doc, "div", "health-detail");
    body.setAttribute("id", "health-detail-" + tier.tier);
    body.hidden = true;

    var facts = node(doc, "dl", "health-fields");
    facts.appendChild(field(doc, "health", tier.health + " (" + tier.healthWord + ")"));
    facts.appendChild(field(doc, "verified", tier.verified ? "yes" : "no"));
    facts.appendChild(field(doc, "selected", tier.slot));
    facts.appendChild(field(doc, "model", tier.model));
    facts.appendChild(field(doc, "provider", tier.provider));
    facts.appendChild(field(doc, "reason", tier.reason));
    if (tier.routingReason) {
      facts.appendChild(field(doc, "routing", tier.routingReason));
    }
    body.appendChild(facts);

    var slots = node(doc, "ul", "health-slots");
    tier.slots.forEach(function (slot) {
      slots.appendChild(slotRow(doc, slot));
    });
    if (tier.slots.length) body.appendChild(slots);

    return body;
  }

  function indicator(doc, tier) {
    var button = node(doc, "button", "health-tier");
    button.type = "button";
    button.setAttribute("data-health", tier.health);
    button.setAttribute("data-verified", tier.verified ? "yes" : "no");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", "health-detail-" + tier.tier);
    // The accessible name carries tier, the backend's health word, and the
    // plain-language meaning, so the state is legible with no colour and no
    // glyph. The glyph itself is hidden from assistive tech for the same
    // reason it is hidden from nobody visually but redundant to a reader.
    button.setAttribute("aria-label", tier.accessibleName);

    var dot = node(doc, "span", "health-dot", tier.glyph);
    dot.setAttribute("aria-hidden", "true");
    button.appendChild(dot);

    return button;
  }

  /* Draw the whole indicator into `container`, replacing what was there.
   *
   * Returns the elements the caller needs to wire: the strip, one button per
   * tier, and one detail panel per tier. Returning them rather than looking
   * them up by id afterwards keeps the wiring honest if the markup changes.
   */
  function renderHealth(doc, container, payload, onSelect) {
    var model = healthModel(payload);

    while (container.firstChild) container.removeChild(container.firstChild);

    var strip = node(doc, "div", "health-strip");
    strip.setAttribute("role", "group");
    strip.setAttribute("aria-label", model.summary);

    var label = node(doc, "span", "health-label", "Models");
    strip.appendChild(label);

    var dots = node(doc, "span", "health-dots");
    strip.appendChild(dots);

    var buttons = {};
    var details = {};

    model.tiers.forEach(function (tier) {
      var button = indicator(doc, tier);
      dots.appendChild(button);
      buttons[tier.tier] = button;

      var panel = detail(doc, tier);
      container.appendChild(panel);
      details[tier.tier] = panel;

      // One open tier at a time. A strip of three tiers with three open panels
      // is a wall of text, and an operator comparing tiers wants them one after
      // another rather than all at once.
      button.addEventListener("click", function () {
        var open = button.getAttribute("aria-expanded") === "true";
        Object.keys(buttons).forEach(function (name) {
          buttons[name].setAttribute("aria-expanded", "false");
          details[name].hidden = true;
        });
        if (!open && typeof onSelect === "function") {
          onSelect(tier);
        }
        if (!open) {
          button.setAttribute("aria-expanded", "true");
          panel.hidden = false;
        }
      });
    });

    var stamp = node(
      doc,
      "span",
      "health-stamp",
      model.verified
        ? "checked " + text(model.checkedAt, "just now")
        : "not verified — configuration only"
    );
    strip.appendChild(stamp);

    container.insertBefore(strip, container.firstChild);

    return { model: model, strip: strip, buttons: buttons, details: details };
  }

  return {
    HEALTH_PRESENTATION: HEALTH_PRESENTATION,
    UNKNOWN_HEALTH: UNKNOWN_HEALTH,
    UNVERIFIED: UNVERIFIED,
    healthModel: healthModel,
    tierModel: tierModel,
    slotModel: slotModel,
    renderHealth: renderHealth,
  };
});