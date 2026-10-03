"""Capability checks for the Workbench HTTP surface.

Three mechanisms, each answering a different question:

``UiToken``
    *Is this request coming from the page Forge itself served?* A random value
    minted per process, injected into ``index.html``, required in a header on
    every capability-bearing request. A cross-site page cannot read it, so it
    cannot forge a header carrying it.

``check_origin``
    *Did this request come from a page Forge served?* An exact string match,
    not a "starts with localhost" check. Localhost is not a security boundary
    on its own: any page in any tab on the machine can issue a request to
    ``http://127.0.0.1:8765`` and a browser will attach its own ``Origin``
    header. Comparing that header to an exact expected value is what
    distinguishes the Workbench page from a page on ``evil.example``.

``StreamTicketStore``
    *May this request open the event stream?* A one-shot, short-lived ticket,
    because ``EventSource`` cannot set a request header. This is the reason the
    main token never appears in a query string: a query string is a URL, and a
    URL leaks into browser history, proxy logs and ``Referer`` headers. The
    ticket grants the stream and nothing else -- it cannot start a run and
    cannot answer an approval.

Two properties are deliberate and worth stating because they are easy to
"improve" in the wrong direction:

*   **No CORS headers, ever.** Not for the page, not for the API, not on error
    responses. A CORS header is a browser-enforced grant to another origin, and
    this surface has no legitimate cross-origin consumer. Absence is the policy.
*   **A missing ``Origin`` is not a match.** Some non-browser clients send
    none, and it would be convenient to treat that as "not a browser, allow
    it". That is exactly the request a forged submission looks like when the
    forger can suppress the header, so absence fails closed.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Iterable

__all__ = [
    "UiToken",
    "StreamTicketStore",
    "check_origin",
    "allowed_origins",
    "TOKEN_HEADER",
    "TICKET_TTL_SECONDS",
    "TOKEN_BYTES",
]

#: Header carrying the UI token. Not ``Authorization``: this is not a bearer
#: credential for a resource, it is a same-origin marker for a local process,
#: and naming it as a token keeps it out of the credential-handling habits that
#: would log it.
TOKEN_HEADER = "X-Forge-Token"

#: Bytes of entropy in the token and in each stream ticket. 32 bytes is far
#: beyond guessing and cheap to generate; the value is per-process, so the
#: threat is a page on the same machine guessing it, not an offline attack.
TOKEN_BYTES = 32

#: How long a stream ticket stays usable. Short by design: it exists to carry a
#: single ``EventSource`` handshake from the page script to the stream, not to
#: be a session credential.
TICKET_TTL_SECONDS = 30.0


class UiToken:
    """The per-process capability token.

    Generated with :mod:`secrets`, so it is cryptographically random rather
    than merely unique. Compared with :func:`hmac.compare_digest` rather than
    ``==``, so a caller cannot learn the token one byte at a time by timing.

    Only one process generates one, and a second process generates a different
    one -- which is why the token doubles as a process identity: a page served
    by a Workbench that has since died cannot be driven by anything holding a
    stale token from the previous run.
    """

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value = secrets.token_urlsafe(TOKEN_BYTES)

    @property
    def value(self) -> str:
        """The token, for embedding in the served page."""
        return self._value

    def matches(self, candidate: str | None) -> bool:
        """Whether ``candidate`` is this token.

        A missing or empty candidate is not a match. ``compare_digest`` requires
        both arguments to be ASCII ``str`` or ``bytes``, so a header carrying
        non-ASCII is rejected before the comparison rather than raising.
        """
        if not candidate:
            return False
        try:
            return hmac.compare_digest(candidate, self._value)
        except (TypeError, UnicodeEncodeError):
            return False

    def __repr__(self) -> str:  # pragma: no cover - operator convenience
        # Never render the value. A token in a traceback or a log line is a
        # token in a file someone will later paste into an issue.
        return "<UiToken redacted>"


def allowed_origins(port: int) -> frozenset[str]:
    """The exact origins this process answers for.

    Both loopback spellings, because ``127.0.0.1`` and ``localhost`` are
    different origins to a browser and an operator may type either. Neither is
    a wildcard: there is no entry that would match a request from anywhere
    else.
    """
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }
    )


def check_origin(origin: str | None, expected: Iterable[str]) -> bool:
    """Whether ``origin`` is exactly one of ``expected``.

    A missing header is a refusal. See the module docstring: absence is the
    shape a header-stripping forger produces, so it cannot mean "allowed".
    """
    if not origin:
        return False
    return origin in frozenset(expected)


class StreamTicketStore:
    """One-shot, short-lived tickets for opening the event stream.

    Needed only because ``EventSource`` is a browser API that cannot set
    headers. The alternative -- putting the main token in the query string --
    would put a capability that can start runs and answer approvals into a URL,
    where it lands in history and logs. A stream ticket is strictly weaker: it
    opens a read-only stream, it is single-use, and it expires in
    :data:`TICKET_TTL_SECONDS`.

    Expiry is checked on redemption rather than by a sweeper thread. A store
    holding a handful of entries does not need a background task to stay
    honest, and a reaper is another thing that can be wrong at 3am.
    """

    def __init__(self, *, ttl: float = TICKET_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._live: dict[str, float] = {}

    def issue(self) -> str:
        """Mint a ticket. Returns the value the caller must present."""
        ticket = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._purge_expired_locked()
            self._live[ticket] = time.monotonic() + self._ttl
        return ticket

    def redeem(self, ticket: str | None) -> bool:
        """Consume ``ticket``. ``True`` for exactly one caller, ever.

        Single-use because a stream ticket is only needed for a handshake: a
        ticket that could be replayed would be a long-lived read capability
        sitting in a URL, which is the problem this class exists to avoid.
        """
        if not ticket:
            return False
        now = time.monotonic()
        with self._lock:
            expiry = self._live.pop(ticket, None)
            self._purge_expired_locked()
        if expiry is None:
            return False
        return now <= expiry

    @property
    def live_count(self) -> int:
        """Unexpired, unredeemed tickets. Exposed for tests and diagnostics."""
        with self._lock:
            self._purge_expired_locked()
            return len(self._live)

    def _purge_expired_locked(self) -> None:
        now = time.monotonic()
        stale = [key for key, expiry in self._live.items() if now > expiry]
        for key in stale:
            del self._live[key]