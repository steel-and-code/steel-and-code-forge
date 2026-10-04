/* A stub DOM for driving health.js under Node.
 *
 * Deliberately minimal and deliberately *not* a DOM implementation. It records
 * what the renderer built so a test can assert on structure, text, and
 * attributes -- which is everything the indicator promises -- and it fails loudly
 * on anything else, so a renderer reaching for a capability this file does not
 * model produces a clear error instead of a silently wrong assertion.
 *
 * `innerHTML` throws rather than existing. health.js builds every element with
 * createElement and textContent precisely so a model id or a provider reason
 * string can never be parsed as markup; a stub that quietly accepted innerHTML
 * would remove the only thing that makes that guarantee testable.
 */

function StubDocument() {
  this.created = 0;
}

StubDocument.prototype.createElement = function (tag) {
  this.created += 1;
  return new StubElement(tag, this);
};

function StubElement(tag, doc) {
  this.tagName = String(tag).toUpperCase();
  this.ownerDocument = doc;
  this.children = [];
  this.attributes = {};
  this.listeners = {};
  this.className = "";
  this.textContent = "";
  this.hidden = false;
  this.parentNode = null;
}

Object.defineProperty(StubElement.prototype, "innerHTML", {
  get: function () {
    throw new Error(
      "health.js must not use innerHTML: a model id or reason string from a " +
        "provider would be parsed as markup"
    );
  },
  set: function () {
    throw new Error("health.js must not use innerHTML");
  },
});

StubElement.prototype.setAttribute = function (name, value) {
  this.attributes[name] = String(value);
};

StubElement.prototype.getAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this.attributes, name)
    ? this.attributes[name]
    : null;
};

/* `removeAttribute` is modelled because the board's delete confirmation reveals
 * itself by removing `hidden`, and a stub that quietly ignored the call would
 * leave the confirmation permanently hidden -- every test below would then pass
 * against a prompt no operator could ever see.
 */
StubElement.prototype.removeAttribute = function (name) {
  delete this.attributes[name];
};

StubElement.prototype.hasAttribute = function (name) {
  return Object.prototype.hasOwnProperty.call(this.attributes, name);
};

StubElement.prototype.appendChild = function (child) {
  child.parentNode = this;
  this.children.push(child);
  return child;
};

StubElement.prototype.insertBefore = function (child, before) {
  child.parentNode = this;
  const index = before ? this.children.indexOf(before) : -1;
  if (index < 0) {
    this.children.push(child);
  } else {
    this.children.splice(index, 0, child);
  }
  return child;
};

StubElement.prototype.removeChild = function (child) {
  const index = this.children.indexOf(child);
  if (index >= 0) this.children.splice(index, 1);
  child.parentNode = null;
  return child;
};

StubElement.prototype.addEventListener = function (name, handler) {
  (this.listeners[name] = this.listeners[name] || []).push(handler);
};

StubElement.prototype.click = function () {
  (this.listeners.click || []).forEach(function (handler) {
    handler({ type: "click" });
  });
};

Object.defineProperty(StubElement.prototype, "firstChild", {
  get: function () {
    return this.children.length ? this.children[0] : null;
  },
});

/* -- traversal helpers, used only by the test ------------------------- */

StubElement.prototype.walk = function (out) {
  const acc = out || [];
  acc.push(this);
  this.children.forEach(function (child) {
    child.walk(acc);
  });
  return acc;
};

StubElement.prototype.findAll = function (predicate) {
  return this.walk().filter(predicate);
};

StubElement.prototype.findOne = function (predicate) {
  const found = this.findAll(predicate);
  if (found.length !== 1) {
    throw new Error(
      "expected exactly one match, found " + found.length
    );
  }
  return found[0];
};

StubElement.prototype.byClass = function (name) {
  return this.findAll(function (el) {
    return String(el.className).split(/\s+/).indexOf(name) >= 0;
  });
};

StubElement.prototype.text = function () {
  // Concatenated text of the subtree, which is what a person actually reads.
  return this.walk()
    .map(function (el) {
      return el.textContent || "";
    })
    .join(" ");
};

module.exports = { StubDocument: StubDocument, StubElement: StubElement };