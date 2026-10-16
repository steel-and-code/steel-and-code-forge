"""Forge Workbench: a local, interactive browser surface for one Byte run.

This package is **optional**. ``forge`` core -- the runtime, the tool
dispatcher, the classifiers, the approval layer -- imports nothing from here,
and nothing here is imported unless an operator runs ``forge workbench`` and has
the ``workbench`` extra installed.

What the surface is, and what it deliberately is not:

*   It is **interactive**. A person is at the browser, so an approval-required
    operation prompts and waits for them -- the same question the terminal
    channel asks, asked through a different transport. See section 3a of
    ``docs/architecture.md`` for why this does not create an unattended approval
    route, and why it does not weaken the fail-closed rule for machine-driven
    HTTP callers.
*   It is **local by default**. Bound to `127.0.0.1` with no host flag, gated by
    a per-process token and a same-origin check on every capability-bearing
    request, and sending no CORS headers at all. The same-origin check is an
    exact ``Origin`` on a POST and, on a read-only GET, an exact ``Origin`` or
    the browser's own ``Sec-Fetch-Site: same-origin`` -- a browser sends no
    ``Origin`` at all on a same-origin GET, so demanding one there would refuse
    the Workbench's own page. See :mod:`forge.ui.security`.
*   It is **local, or one named LAN address, by explicit request**.
    ``forge workbench --lan --lan-address ADDR`` serves the same surface on one
    interface for a tablet on the same network, and adds what a
    page-fetching peer does not already have: an operator-set passphrase gating
    ``GET /``, a ``Host`` allowlist against DNS rebinding, per-address unlock
    backoff, and a **read-only** default in which every POST is refused unless
    ``--lan-steer`` is also passed. There is no ``--host``, and no value means
    "any interface". The token was never an authentication boundary off
    loopback, which is the fact the passphrase exists to fix.
*   It is **one session**. One process, one active run. A second task while a
    run is in flight is a conflict, not a queue.
*   It is **inert**. It observes through :class:`forge.events.EventSink` and
    decides nothing: approvals remain the single-spend capability in
    ``forge/tools/approval.py``, and no module here may import a classifier or
    call an ``Executor`` directly.

The modules divide as:

``security``
    Capability checks. The UI token, the same-origin rules (exact ``Origin``
    for state-changing requests, and for read-only ones an exact ``Origin``
    *or* the browser's ``Sec-Fetch-Site``), and the short-lived stream ticket
    that lets ``EventSource`` open an SSE connection without putting the main
    token in a query string.
``broker``
    Thread-safe fan-out of events to any number of SSE subscribers, with
    bounded per-subscriber queues and an in-memory replay ring.
``channel``
    :class:`~forge.tools.approval.ApprovalChannel` for a browser. Synchronous,
    like every other channel: it blocks the *worker* thread, never the event
    loop.
``session``
    One active run, its state machine, and the worker thread it runs on.
``runner``
    Composition. Builds a gateway, a dispatcher and a runtime, exactly as the
    CLI does, and decides nothing.
``server``
    The FastAPI routes and the static page.
"""

from __future__ import annotations

__all__ = ["UiUnavailable"]


class UiUnavailable(RuntimeError):
    """Raised when the Workbench extras are not installed.

    A distinct exception rather than an ``ImportError`` so the CLI can explain
    the fix in a sentence instead of printing a traceback: this is a missing
    optional dependency, not a bug.
    """