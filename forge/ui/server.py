"""The Workbench HTTP surface: SSE for activity, POST for decisions.

Six routes, and the split between them is the design: an **observation** reads
state the server already holds, a **command** changes something.

``GET  /api/status``
    Everything true right now, including any pending approval. Token plus the
    read-only same-origin check -- it is the endpoint a refresh depends on, so
    it carries the same capability as the pages that read it. The read-only
    check is a *pair*: exact ``Origin`` when the browser sent one, else
    ``Sec-Fetch-Site: same-origin``, because a same-origin GET carries no
    ``Origin`` at all. See :mod:`forge.ui.security` for the measurement.

``GET  /api/models``
    The cached model-health snapshot, one entry per capability tier, taken from
    ``ModelRouter.status()`` verbatim. **Strictly read-only and never probing**:
    a page load, a refresh, or an operator reloading cannot spend a provider
    request or present a credential. Same read-only guard as ``GET /api/status``,
    because it is an observation like that one.

``POST /api/models/verify``
    One bounded verification pass, then the new snapshot. A **command**, so it
    takes the strict exact-Origin and token guards rather than the read-only
    pair, and refuses while a run is in flight. See
    :meth:`create_app`'s route for why that refusal is load-bearing.

``GET  /api/events``
    The activity stream. Read-only, and gated by a short-lived single-use
    ticket rather than the main token, because ``EventSource`` cannot set a
    header and the main token can start runs and answer approvals.

``POST /api/run``
    Start one task. ``202`` with a run id; the run itself happens on a worker
    thread, so the response returns immediately and the stream carries what
    happens next.

``POST /api/approvals/{id}``
    The decision. A command, not an event, so it is a POST rather than a frame
    on the stream.

Three properties of the whole surface, each of which is a decision:

*   **The event loop is never blocked waiting for a human.** ``present()`` waits
    on the worker thread; this module only awaits its own queue. That is why the
    approval POST can be served while the run that asked the question is still
    waiting for it.
*   **No CORS headers, on any response.** Not as a setting to be turned on; there
    is no code path that adds one.
*   **The API key never reaches the browser.** The gateway is constructed in the
    server process from the environment, the page receives only the loopback URL
    and the UI token, and ``index.html`` contains no reference to any provider
    credential.

Static files are served as files. The only transformation is substituting the
per-process token into the page at request time, which is a single string
replacement rather than a template engine -- one less dependency for a page
that has exactly one variable.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..models.base import ModelGateway
from ..tools.workspace import Workspace
from .broker import HEARTBEAT_SECONDS, SseBroker, Subscriber
from .channel import StaleApproval, UnknownApproval, WebApprovalChannel
from .security import (
    SEC_FETCH_SITE_HEADER,
    TOKEN_HEADER,
    StreamTicketStore,
    UiToken,
    allowed_origins,
    check_origin,
    check_same_origin,
)
from .models import HealthUnavailable, ModelHealth
from .session import SessionBusy, UiSessionState, WorkbenchSession

__all__ = ["create_app", "STATIC_DIR", "TOKEN_PLACEHOLDER"]

#: Where the three static files live. Next to this module rather than at the
#: package root, so the optional UI package owns everything it serves.
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: What the served page carries in place of the real token. A visible
#: placeholder rather than a silent ``None``: if the substitution ever fails,
#: the page's token is obviously wrong instead of mysteriously empty.
TOKEN_PLACEHOLDER = "__FORGE_UI_TOKEN__"


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class RunRequest(BaseModel):
    """``POST /api/run`` body."""

    task: str = Field(min_length=1, max_length=8000)


class ApprovalRequestBody(BaseModel):
    """``POST /api/approvals/{id}`` body.

    ``decision`` is an explicit enum rather than a boolean so a caller cannot
    pass anything at all -- a truthy string from a confused client would
    otherwise be an approval, and this is the one endpoint where that would
    matter.
    """

    decision: str

    def approved(self) -> bool:
        if self.decision not in ("yes", "no"):
            raise HTTPException(
                status_code=422,
                detail="decision must be exactly 'yes' or 'no'",
            )
        return self.decision == "yes"


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------


def create_app(
    *,
    workspace: Workspace,
    session: WorkbenchSession,
    gateway: ModelGateway,
    token: UiToken,
    port: int,
    health: ModelHealth | None = None,
) -> FastAPI:
    """Build the Workbench application.

    Args:
        workspace: The run's fixed root. Held for reporting only: no request can
            change it, because a browser-supplied path would undermine every
            other control.
        session: The one active run.
        gateway: Held so the credential lives in the server process alone and
            is never reachable from a page.
        token: The per-process UI token, injected into the served page.
        port: The port this process is bound to, which is what an exact
            ``Origin`` must equal.
        health: The cached model-health snapshot the page's indicator reads.
            Optional, so a surface built without a model registry still serves
            the page. ``GET /api/models`` then answers 503 with the reason
            rather than the indicator showing an empty row an operator would
            read as healthy.

    Deliberately constructed with explicit arguments rather than read from
    ``os.environ`` or a global: a test needs to build two independent
    applications in one process, each with its own token and workspace.
    """
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Bind the broker's loop at startup, release pending approvals at exit.

        A lifespan handler rather than ``on_event`` because the latter is
        deprecated, and a deprecation warning on every server start trains
        people to ignore warnings.
        """
        # Binding the loop is what makes delivery work at all: without it the
        # page loads, the stream connects, and nothing ever arrives -- a
        # failure indistinguishable from an idle run.
        broker.bind_loop(asyncio.get_running_loop())
        try:
            yield
        finally:
            # Refuse anything pending, so shutdown is a clean exit rather than
            # a worker blocked forever on an event nobody will set.
            session.shutdown()

    app = FastAPI(
        title="Forge Workbench",
        # No docs UI. This is a local operator surface, and /docs would be an
        # unneeded page rendering endpoint documentation to a browser.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    broker = session.broker
    tickets = StreamTicketStore()
    origins = allowed_origins(port)

    # Exposed on app.state rather than kept as a closure local, so a test can
    # assert the single-use property against the store the routes actually use
    # rather than against a store it constructed itself.
    app.state.stream_tickets = tickets
    app.state.workbench_session = session
    app.state.model_health = (
        health if health is not None else ModelHealth(None)
    )

    # -- guards ---------------------------------------------------------

    def require_browser(request: Request) -> None:
        """Exact-Origin check, on every state-changing route.

        A page served from ``evil.example`` can make the browser issue a
        request to ``http://127.0.0.1:8765``, and the browser will attach the
        *attacker's* Origin. Comparing it to an exact expected value is the
        whole defence; a missing header fails closed.

        Only for POSTs. A browser does not send ``Origin`` on a same-origin
        GET, so requiring it there would refuse the Workbench's own page; the
        read-only routes use :func:`require_same_origin` instead.
        """
        if not check_origin(request.headers.get("origin"), origins):
            raise HTTPException(
                status_code=403,
                detail="origin not allowed; this surface serves one page only",
            )

    def require_same_origin(request: Request) -> None:
        """The read-only check: exact ``Origin``, else browser-attested same-origin.

        A same-origin ``fetch`` GET and an ``EventSource`` GET arrive with no
        ``Origin`` header, so ``Origin`` alone cannot answer this. An exact
        match still wins when present (refusing a cross-origin GET carrying an
        attacker ``Origin``); otherwise ``Sec-Fetch-Site: same-origin`` is
        required. Both absent is a refusal.
        """
        if not check_same_origin(
            request.headers.get("origin"),
            request.headers.get(SEC_FETCH_SITE_HEADER),
            origins,
        ):
            raise HTTPException(
                status_code=403,
                detail="origin not allowed; this surface serves one page only",
            )

    def require_token(request: Request) -> None:
        """The per-process UI token, compared in constant time."""
        if not token.matches(request.headers.get(TOKEN_HEADER)):
            raise HTTPException(status_code=403, detail="invalid UI token")

    async def guarded(request: Request) -> None:
        """Origin + token, for state-changing requests."""
        await asyncio.to_thread(require_browser, request)
        await asyncio.to_thread(require_token, request)

    async def guarded_read(request: Request) -> None:
        """Same-origin + token, for read-only requests.

        The token is still required. ``Sec-Fetch-Site`` answers "was this the
        page I served", not "is this caller allowed" -- dropping the token here
        would let any same-origin script read the workspace and any pending
        approval, which is exactly the data the token exists to gate.
        """
        await asyncio.to_thread(require_same_origin, request)
        await asyncio.to_thread(require_token, request)

    # -- page -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """The single page, with the token substituted in.

        Served without a token requirement: it *is* the token's delivery
        mechanism, so requiring it would be circular. It carries no capability
        beyond what it hands to the browser that Forge itself sent it to.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(TOKEN_PLACEHOLDER, token.value))

    if STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

    # -- API ------------------------------------------------------------

    @app.post("/api/events-ticket")
    async def events_ticket(
        request: Request, _: None = Depends(guarded)
    ) -> JSONResponse:
        """Mint a single-use, short-lived ticket for the event stream.

        Separate from ``GET /api/events`` so the main token never has to travel
        in a query string. This is the one request a page makes with the token
        *in a header* to obtain something it can use without one.
        """
        return JSONResponse({"ticket": tickets.issue()})

    @app.get("/api/status")
    async def api_status(
        request: Request, _: None = Depends(guarded_read)
    ) -> JSONResponse:
        """Current state, including any pending approval."""
        return JSONResponse(session.status())

    @app.get("/api/models")
    async def api_models(
        request: Request, _: None = Depends(guarded_read)
    ) -> JSONResponse:
        """The cached model-health snapshot, one entry per capability tier.

        **Strictly read-only.** This route returns what
        :class:`~forge.ui.models.ModelHealth` already holds and never calls
        into a probe, so a page load, a refresh, or an operator hammering F5
        cannot spend a provider request or present a credential. Refreshing the
        indicator re-reads this; it never re-checks.

        The entries are ``ModelRouter.status()`` verbatim -- no renamed fields,
        no derived booleans, no filtering -- so this cannot disagree with
        ``forge models status`` about what a tier's health is.

        503 when no registry was loaded, with the reason. An empty indicator
        would read as "no models configured and nothing wrong", which is a
        different statement from "Forge could not read your configuration".
        """
        model_health: ModelHealth = app.state.model_health
        if not model_health.available:
            return JSONResponse({"detail": model_health.error}, status_code=503)
        return JSONResponse(model_health.status())

    @app.post("/api/models/verify")
    async def api_models_verify(
        request: Request, _: None = Depends(guarded)
    ) -> JSONResponse:
        """Re-check model health: one bounded verification pass, then the new snapshot.

        A POST because it is a command, not an observation. It reaches providers
        and presents credentials, so it takes the strict exact-Origin and token
        guards rather than the read-only pair -- and a browser cannot trigger it
        from a page it did not load.

        Three behaviours worth stating:

        *   **Refused while a run is in flight** (409). A verification pass
            opens sockets from inside a process that is already executing; that
            would be an incidental side effect of running Byte, and health
            checking must never be one. Not queued either: the operator asked a
            question, and an answer from after the run is a different answer.
        *   **Bounded by the verifier layer, not here.** One attempt per
            distinct model, no retries, five seconds per attempt. This route
            holds no timeout of its own.
        *   **Credentials never leave the server.** The ``environ`` mapping lives
            in :class:`~forge.ui.models.ModelHealth` and is read only by the
            verifier factories. The response is the same status payload the read
            route returns.
        """
        if session.busy:
            return JSONResponse(
                {
                    "detail": (
                        "a run is in progress; re-check model health while the "
                        "workbench is idle"
                    )
                },
                status_code=409,
            )

        model_health: ModelHealth = app.state.model_health
        try:
            # Off the event loop: the probe blocks for as long as its own
            # timeout allows, and an operator waiting on it must still be able
            # to read status and answer an approval.
            body = await asyncio.to_thread(model_health.verify)
        except HealthUnavailable as unavailable:
            return JSONResponse({"detail": str(unavailable)}, status_code=503)

        return JSONResponse(body)

    @app.post("/api/run")
    async def api_run(
        body: RunRequest, request: Request, _: None = Depends(guarded)
    ) -> JSONResponse:
        """Start one run on a worker thread.

        ``202``: accepted and running, not finished. The response says the run
        started, and the stream carries what it did.
        """
        try:
            run_id = session.submit(body.task)
        except SessionBusy as busy:
            # 409, not a queue. One process, one run; pretending otherwise would
            # imply a task the operator asked for is going to happen.
            return JSONResponse(
                {"detail": str(busy), "run_id": busy.run_id, "state": session.state.value},
                status_code=409,
            )
        except ValueError as invalid:
            raise HTTPException(status_code=422, detail=str(invalid)) from invalid

        return JSONResponse(
            {"run_id": run_id, "state": session.state.value}, status_code=202
        )

    @app.post("/api/approvals/{approval_id}")
    async def api_approve(
        approval_id: str,
        body: ApprovalRequestBody,
        request: Request,
        _: None = Depends(guarded),
    ) -> JSONResponse:
        """Record a human decision and wake the worker.

        Three outcomes, and the distinction between them is the point:

        * ``200`` -- accepted. Exactly one caller per approval ever gets here.
        * ``409`` -- already answered. A second tab gets this rather than a
          success it would read as "my click decided it".
        * ``404`` -- no such pending approval.

        Returns immediately. The worker it wakes runs on its own thread, and
        this coroutine returns before any of that happens.
        """
        approved = body.approved()
        channel: WebApprovalChannel = session.channel
        try:
            channel.respond(approval_id, approved)
        except StaleApproval as stale:
            return JSONResponse({"detail": str(stale)}, status_code=409)
        except UnknownApproval as unknown:
            return JSONResponse({"detail": str(unknown)}, status_code=404)

        return JSONResponse({"approval_id": approval_id, "state": session.state.value})

    @app.get("/api/events")
    async def api_events(
        request: Request,
        ticket: str | None = None,
        last_event_id: str | None = None,
    ) -> Response:
        """The SSE stream.

        Gated by a stream ticket plus the read-only same-origin check. The
        ticket opens a read-only stream; it cannot start a run and cannot
        answer an approval, which is why it is safe to carry in a URL.

        The order is load-bearing: the same-origin check runs *before*
        ``redeem``, so a cross-origin request is refused without spending a
        ticket. Otherwise any page on the machine could burn an operator's
        ticket before the operator's own ``EventSource`` ever connected.

        ``Last-Event-ID`` is honoured by header or query so a reconnecting
        browser gets the tail it missed. When the ring has moved past the gap,
        the stream says so rather than presenting a partial history as a whole
        one.
        """
        await asyncio.to_thread(require_same_origin, request)
        if not tickets.redeem(ticket):
            raise HTTPException(
                status_code=403,
                detail="missing, expired, or already-used stream ticket",
            )

        cursor = _last_seq(last_event_id)
        subscriber = broker.subscribe()
        return StreamingResponse(
            _stream(broker, subscriber, cursor),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                # Not required for same-origin EventSource, and stated so a
                # future reader does not add it thinking it helps.
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/health")
    async def api_health() -> JSONResponse:
        """Liveness, unauthenticated and capability-free.

        Useful for a human checking the port is live, and deliberately reveals
        nothing: not the workspace, not the model, not whether a run is active.
        """
        return JSONResponse({"ok": True, "service": "forge-workbench"})

    return app


# --------------------------------------------------------------------------
# SSE plumbing
# --------------------------------------------------------------------------


def _last_seq(raw: str | None) -> int:
    """Parse a ``Last-Event-ID`` value. ``0`` when absent or unparseable.

    Unparseable means "send me everything you still have" rather than an error:
    a browser sending a malformed cursor should still get a working stream.
    """
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value >= 0 else 0


def _frame(seq: int, name: str, body: dict[str, object]) -> str:
    """One SSE frame.

    The event name is carried in the ``event:`` field and repeated inside the
    JSON payload, so a consumer can dispatch either way. Redundant on purpose:
    ``EventSource.onmessage`` only fires for unnamed events, and a client using
    ``addEventListener`` needs the name -- supporting both without forcing one
    style on the page.
    """
    payload = json.dumps({"seq": seq, "event": name, **body}, separators=(",", ":"))
    return f"id: {seq}\nevent: {name}\ndata: {payload}\n\n"


async def _stream(
    broker: SseBroker,
    subscriber: Subscriber,
    cursor: int,
) -> AsyncIterator[str]:
    """Yield SSE frames until the client goes away.

    Unsubscribing is in a ``finally`` that wraps the *whole* body, replay
    included. That placement is load-bearing: a generator closed before it
    reaches the live loop -- during replay, or between the first and second
    frame -- would never run a ``finally`` that only guarded the loop, and the
    subscriber would stay registered for a browser that is gone, holding a queue
    slot and receiving every subsequent event.

    A generator is closed on disconnect, on cancellation, and on an exception,
    so all three paths land here.
    """
    try:
        pending, complete = broker.replay_since(cursor)
        if not complete:
            yield _frame(
                0,
                "stream.truncated",
                {"detail": "replay ring moved past this cursor"},
            )
        for seq, event in pending:
            yield _frame(seq, event.name, dict(event.fields))

        # A comment frame, unconditionally, before the first live event.
        #
        # Not decoration. Without an immediate byte, a stream that opens during
        # a quiet run sends nothing until something happens, and a client
        # waiting on the response head blocks -- which looks exactly like a
        # broken server. It also flushes the headers through an intermediary
        # that buffers a response it has not seen the start of.
        yield ": stream open\n\n"

        while True:
            try:
                seq, event = await asyncio.wait_for(
                    subscriber.queue.get(), timeout=HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                # A comment frame. Keeps intermediaries from reaping an idle
                # stream and gives the client a liveness signal.
                yield ": keep-alive\n\n"
                continue

            missed = subscriber.take_dropped()
            if missed:
                # Say the gap happened. A page that silently missed an
                # approval prompt would look broken with no way to tell why.
                yield _frame(seq, "stream.dropped", {"dropped": str(missed)})
            yield _frame(seq, event.name, dict(event.fields))
    finally:
        broker.unsubscribe(subscriber)


#: Session states the page may treat as terminal. Exported so a test can assert
#: the set has not quietly grown a ``cancelled``.
TERMINAL_STATES = frozenset(
    {UiSessionState.COMPLETED, UiSessionState.FAILED}
)