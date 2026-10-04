/* Forge Workbench — the Order Board.
 *
 * This file exists apart from app.js for the same reason health.js does: its
 * decisions are testable without a browser. Everything here is a pure function
 * over the payload `GET /api/orders` returns, plus one renderer that takes a
 * `document`. The Node test in `tests/test_ui_orders_board_ui.py` drives both
 * against a stub DOM.
 *
 * Four rules this file keeps, and each is load-bearing:
 *
 *  - IT NEVER INVENTS A WORKFLOW STATE. The backend publishes `statuses` in board
 *    order and one column per state, and this file renders exactly those. There
 *    is no list of states here to fall out of step with `forge.ui.orders`, and a
 *    state Forge does not know is rendered as an explicit "unknown state" rather
 *    than dropped -- silently omitting a column would look like a bug in the
 *    board and be one in the page.
 *
 *  - THE BLACKSMITH WORDING IS PRESENTATION, NOT DATA. The stable identifiers
 *    (`orders_received`, `at_the_forge`) are what travel over the wire and what
 *    the backend validates. The words an operator reads ("On The Rack") are a
 *    label table here. A rename of a column is a one-line change in this file
 *    and touches no stored order.
 *
 *  - COLOUR IS NEVER THE ONLY SIGNAL. Priority carries a glyph that differs per
 *    level, the visible word, and a `data-priority` attribute. An orange card and
 *    a red card are the same rectangle to a colour-blind operator, and priority
 *    is the field they most need to sort by.
 *
 *  - NO STORAGE AND NO INVENTED STATE. Nothing here is written anywhere; a
 *    refresh re-reads `GET /api/orders` and draws the server's answer. There is
 *    no drag-and-drop and no delete: movement is an explicit control, and there
 *    is no delete in this MVP.
 */

(function (root, factory) {
  "use strict";

  var api = factory();

  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.ForgeBoard = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /* Presentation only, keyed by the backend's stable identifiers.
   *
   * `glyph` exists so priority is legible with no colour. It is decoration and is
   * hidden from assistive technology, exactly as the health indicator's glyph is
   * -- the accessible name carries the word, and a screen reader announcing
   * "black circle" instead of "urgent" would make the board worse than useless.
   */
  var PRIORITY_PRESENTATION = {
    low: { word: "low", glyph: "↓", rank: 0 },
    normal: { word: "normal", glyph: "–", rank: 1 },
    high: { word: "high", glyph: "↑", rank: 2 },
    urgent: { word: "urgent", glyph: "!!", rank: 3 },
  };

  /* Shown when the backend sends a priority this build has never heard of.
   *
   * Rendered as its own visible state rather than defaulting to a colour, for
   * the same reason an unknown health string does not default to green.
   */
  var UNKNOWN_PRIORITY = { word: "unknown priority", glyph: "?", rank: -1 };

  /* Shown when the backend sends a status this build has never heard of.
   *
   * The column still renders. Hiding it would make an operator believe the
   * board has no such column, when in fact an order is sitting in it.
   */
  var UNKNOWN_STATUS = { word: "unknown state", glyph: "?" };

  function priorityOf(priority) {
    return PRIORITY_PRESENTATION[priority] || UNKNOWN_PRIORITY;
  }

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") {
      return fallback === undefined ? "" : fallback;
    }
    return String(value);
  }

  /* An identifier turned into a heading, when the backend sent no label.
   *
   * `orders_received` -> `Orders received`. This is a last resort, not the
   * normal path: `column.label` is published by the backend, and this only runs
   * for a payload that predates it or a state the backend has added without a
   * label. The alternative -- an empty heading -- is worse than an ugly one.
   */
  function titleise(identifier) {
    var words = String(identifier || "").replace(/[_-]+/g, " ").trim();
    if (!words) return "Unnamed column";
    return words.charAt(0).toUpperCase() + words.slice(1);
  }

  /* One order, as the page draws a card.
   *
   * `accessibleName` carries number, title, priority, and smith, because those
   * are the four things a card shows and a screen-reader user should hear all of
   * them without navigating into it.
   */
  function cardModel(order) {
    var priority = priorityOf(order.priority);
    var smith = text(order.smith, "");
    var number = text(order.order_id, "no number");

    return {
      orderId: number,
      title: text(order.title, "untitled order"),
      status: text(order.status, ""),
      statusLabel: text(order.status_label, ""),
      priority: text(order.priority, ""),
      priorityWord: priority.word,
      priorityGlyph: priority.glyph,
      priorityRank: priority.rank,
      smith: smith,
      unassigned: !smith,
      description: text(order.description, ""),
      createdAt: text(order.created_at, ""),
      sequence: Number(order.sequence) || 0,
      accessibleName:
        number + ": " +
        text(order.title, "untitled order") +
        ", priority " +
        priority.word +
        (smith ? ", smith " + smith : ", unassigned"),
    };
  }

  function columnModel(column) {
    var status = text(column.status, "");
    var orders = (column.orders || []).map(cardModel);
    // A column the backend labelled has a real heading; one it did not label
    // falls back to the identifier turned into words. That fallback is also how
    // a state this build has never seen is rendered -- as a named column rather
    // than dropped, because an order sitting in it is real.
    var label = text(column.label, "");
    var labelled = !!label;
    if (!labelled) label = titleise(status);

    return {
      status: status,
      label: label,
      // Derived from the payload, not from a list of states this file knows
      // about. There is deliberately no workflow-state table here: that is the
      // backend's, and a second one in the frontend is a second opinion that can
      // disagree about what states exist.
      labelled: labelled,
      count: typeof column.count === "number" ? column.count : orders.length,
      orders: orders,
      emptyMessage: "No orders in " + label + ".",
      // Read by assistive tech before the heading and the cards: how many orders,
      // and in what state. "At The Forge" on its own does not say whether it is
      // busy.
      summary:
        label + ": " +
        orders.length +
        (orders.length === 1 ? " order" : " orders"),
    };
  }

  /* The whole board.
   *
   * `columns` is taken from the payload in the order it arrived, which is the
   * backend's workflow order. Nothing here sorts, filters, or reorders it.
   */
  function boardModel(payload) {
    var body = payload || {};
    var columns = (body.columns || []).map(columnModel);
    var total = columns.reduce(function (sum, column) {
      return sum + column.orders.length;
    }, 0);

    return {
      columns: columns,
      // Counted from the columns rather than read off the payload, so the board
      // has one number it can state without trusting a second field that the
      // backend derived from the same columns in the first place.
      total: total,
      nextId: text(body.next_id, ""),
      statuses: (body.statuses || []).slice(),
      priorities: (body.priorities || []).slice(),
    };
  }

  // -- rendering ---------------------------------------------------------
  //
  // `doc` is passed in rather than reached for globally, so this runs under a
  // stub document in the test. Everything is built with createElement and
  // textContent: no innerHTML, so a title or a description an operator typed can
  // never be parsed as markup.

  function node(doc, tag, className, textContent) {
    var element = doc.createElement(tag);
    if (className) element.className = className;
    if (textContent !== undefined && textContent !== null) {
      element.textContent = String(textContent);
    }
    return element;
  }

  function priorityTag(doc, card) {
    var tag = node(doc, "span", "board-priority");
    tag.setAttribute("data-priority", card.priority);

    var glyph = node(doc, "span", "board-priority-glyph", card.priorityGlyph);
    glyph.setAttribute("aria-hidden", "true");
    tag.appendChild(glyph);

    tag.appendChild(node(doc, "span", "board-priority-word", card.priorityWord));
    return tag;
  }

  function card(doc, cardModelValue) {
    var button = node(doc, "button", "board-card");
    button.type = "button";
    button.setAttribute("data-order-id", cardModelValue.orderId);
    button.setAttribute("data-priority", cardModelValue.priority);
    button.setAttribute("data-status", cardModelValue.status);
    button.setAttribute("aria-label", cardModelValue.accessibleName);

    var head = node(doc, "span", "board-card-head");
    head.appendChild(node(doc, "span", "board-number", cardModelValue.orderId));
    head.appendChild(priorityTag(doc, cardModelValue));
    button.appendChild(head);

    button.appendChild(node(doc, "span", "board-card-title", cardModelValue.title));

    var foot = node(doc, "span", "board-card-foot");
    foot.appendChild(
      node(
        doc,
        "span",
        "board-smith" + (cardModelValue.unassigned ? " board-unassigned" : ""),
        cardModelValue.unassigned ? "unassigned" : cardModelValue.smith
      )
    );
    button.appendChild(foot);

    return button;
  }

  function column(doc, columnModelValue, handlers) {
    var section = node(doc, "section", "board-column");
    section.setAttribute("data-status", columnModelValue.status);
    // A dashed edge marks a column the backend did not label, so a state added
    // without one is visibly provisional to whoever is looking at it.
    if (!columnModelValue.labelled) section.setAttribute("data-unlabelled", "yes");

    var head = node(doc, "div", "board-column-head");
    var heading = node(doc, "h3", "board-column-title", columnModelValue.label);
    head.appendChild(heading);

    // The count is text beside the heading, not a coloured badge. A number an
    // operator can read is the same information a colour would have carried and
    // is legible without it.
    var count = node(doc, "span", "board-column-count", String(columnModelValue.count));
    count.setAttribute("aria-hidden", "true");
    head.appendChild(count);
    section.appendChild(head);

    // The summary is what assistive tech reads in place of the bare heading: the
    // label plus how many orders it holds.
    var summary = node(doc, "p", "sr-only", columnModelValue.summary);
    section.appendChild(summary);

    var list = node(doc, "ul", "board-cards");
    list.setAttribute("aria-label", columnModelValue.label);

    // Collected as they are built and handed back, rather than found afterwards
    // by walking the tree: a lookup helper here would be a test-double method
    // that does not exist in a browser.
    var cards = {};

    if (columnModelValue.orders.length === 0) {
      var empty = node(doc, "li", "board-empty", columnModelValue.emptyMessage);
      list.appendChild(empty);
    } else {
      columnModelValue.orders.forEach(function (value) {
        var item = node(doc, "li", "board-card-slot");
        var button = card(doc, value);
        if (handlers && typeof handlers.onOpen === "function") {
          button.addEventListener("click", function () {
            handlers.onOpen(value);
          });
        }
        item.appendChild(button);
        list.appendChild(item);
        cards[value.orderId] = button;
      });
    }
    section.appendChild(list);

    return { section: section, cards: cards };
  }

  /* Draw the board into `container`, replacing what was there.
   *
   * Returns the elements the caller needs to wire: the column elements, keyed by
   * status, and the card buttons keyed by order id. Returning them rather than
   * looking them up by id afterwards keeps the wiring honest if the markup
   * changes.
   */
  function renderBoard(doc, container, payload, handlers) {
    var model = boardModel(payload);

    while (container.firstChild) container.removeChild(container.firstChild);

    var columnsNode = node(doc, "div", "board-columns");
    var columnElements = {};
    var cardElements = {};

    model.columns.forEach(function (columnModelValue) {
      var built = column(doc, columnModelValue, handlers);
      columnsNode.appendChild(built.section);
      columnElements[columnModelValue.status] = built.section;
      Object.keys(built.cards).forEach(function (orderId) {
        cardElements[orderId] = built.cards[orderId];
      });
    });

    container.appendChild(columnsNode);

    return {
      model: model,
      columnsNode: columnsNode,
      columns: columnElements,
      cards: cardElements,
    };
  }

  /* One order's details, as a dialog.
   *
   * `states` is the backend's own vocabulary, as `{status, label}` pairs taken
   * from `payload.columns`. Passing labels through rather than titleising the
   * identifiers here is what keeps one set of words in the surface: a column
   * heading and a move button must not spell the same state two ways, and the
   * page is not entitled to its own wording for it. A pair with no label falls
   * back to a titleised identifier, which is what an unlabelled column heading
   * does too.
   */
  function renderDetail(doc, container, order, states, handlers) {
    var model = order ? cardModel(order) : null;

    while (container.firstChild) container.removeChild(container.firstChild);
    if (!model) return { model: null };

    var available = (states || []).map(function (entry) {
      return typeof entry === "string"
        ? { status: entry, label: titleise(entry) }
        : {
            status: entry.status,
            label: text(entry.label, "") || titleise(entry.status),
          };
    });

    var dialog = node(doc, "section", "order-detail");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "order-detail-title");
    dialog.setAttribute("data-order-id", model.orderId);

    var heading = node(doc, "h2", "order-detail-title", model.orderId);
    heading.setAttribute("id", "order-detail-title");
    dialog.appendChild(heading);

    var title = node(doc, "p", "order-detail-title-line", model.title);
    dialog.appendChild(title);

    var facts = node(doc, "dl", "order-detail-facts");
    [
      ["priority", model.priorityWord],
      ["smith", model.smith || "unassigned"],
      ["state", model.statusLabel || titleise(model.status)],
      ["received", model.createdAt || "not recorded"],
    ].forEach(function (pair) {
      var wrapper = node(doc, "div", "order-detail-field");
      wrapper.appendChild(node(doc, "dt", null, pair[0]));
      wrapper.appendChild(node(doc, "dd", "mono", pair[1]));
      facts.appendChild(wrapper);
    });
    dialog.appendChild(facts);

    if (model.description) {
      dialog.appendChild(
        node(doc, "p", "order-detail-description", model.description)
      );
    }

    var move = node(doc, "div", "order-move");
    move.setAttribute("role", "group");
    move.setAttribute(
      "aria-label",
      "Move " + model.orderId + " to another state"
    );
    available.forEach(function (entry) {
      var button = node(doc, "button", "order-move-button", entry.label);
      button.type = "button";
      button.setAttribute("data-target-status", entry.status);
      button.setAttribute(
        "aria-label",
        "Move " + model.orderId + " to " + entry.label
      );
      if (entry.status === model.status) {
        button.setAttribute("aria-current", "true");
        button.disabled = true;
      }
      if (handlers && typeof handlers.onMove === "function") {
        button.addEventListener("click", function () {
          handlers.onMove(model.orderId, entry.status);
        });
      }
      move.appendChild(button);
    });
    dialog.appendChild(move);

    container.appendChild(dialog);
    return { model: model, dialog: dialog, move: move };
  }

  return {
    PRIORITY_PRESENTATION: PRIORITY_PRESENTATION,
    UNKNOWN_PRIORITY: UNKNOWN_PRIORITY,
    UNKNOWN_STATUS: UNKNOWN_STATUS,
    boardModel: boardModel,
    columnModel: columnModel,
    cardModel: cardModel,
    priorityOf: priorityOf,
    titleise: titleise,
    renderBoard: renderBoard,
    renderDetail: renderDetail,
  };
});