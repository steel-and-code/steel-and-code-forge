"""Capability checks for the Workbench HTTP surface.

Three mechanisms, each answering a different question:

``UiToken``
    *Is this request coming from the page Forge itself served?* A random value
    minted per process, injected into ``index.html``, required in a header on
    every capability-bearing request. A cross-site page cannot read it, so it
    cannot forge a header carrying it.

``check_origin``
    *Did this state-changing request come from a page Forge served?* An exact
    string match, not a "starts with localhost" check. Localhost is not a
    security boundary on its own: any page in any tab on the machine can issue
    a request to ``http://127.0.0.1:8765`` and a browser will attach its own
    ``Origin`` header. Comparing that header to an exact expected value is what
    distinguishes the Workbench page from a page on ``evil.example``.

``check_same_origin``
    *Did this read-only request come from the page Forge served?* ``Origin`` is
    mandatory on a POST and **optional on a same-origin GET**, so ``Origin``
    alone cannot answer this question. See the module-level note on what a
    browser actually sends.

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
*   **A missing ``Origin`` is not a match on a state-changing request.** Some
    non-browser clients send none, and it would be convenient to treat that as
    "not a browser, allow it". That is exactly the request a forged submission
    looks like when the forger can suppress the header, so absence fails
    closed.

What a browser actually sends
-----------------------------

The rule above is about POSTs, and the distinction is not pedantry -- it is the
difference between a working page and a dead one. Measured against Chrome 154
on the wire, for a page served from ``http://127.0.0.1:8765``:

===================  ===================  ===================  ==========
Request              ``Origin``           ``Sec-Fetch-Site``   ``Referer``
===================  ===================  ===================  ==========
``GET /api/status``  **absent**           ``same-origin``      present
``GET /api/events``  **absent**           ``same-origin``      present
(``EventSource``)
``POST /api/run``    present              ``same-origin``      present
===================  ===================  ===================  ==========

A browser sends ``Origin`` on a same-origin request when the *method* makes the
answer relevant -- it is the CORS-preflight and CSRF signal, and a GET carries
no cross-origin capability worth reporting. So a same-origin ``fetch`` GET and
a same-origin ``EventSource`` GET both arrive with **no ``Origin`` at all**,
while every POST from the same page carries one. Requiring an exact ``Origin``
on those GETs does not fail closed, it fails *always*: the page's own requests
are indistinguishable from a forger's, and the UI sits there refusing its own
authorised caller.

``Sec-Fetch-Site`` is the header that *is* sent on every one of those requests.
It is a Fetch Metadata request header, which makes it the right signal here for
two reasons:

*   it is present on same-origin GETs, which is exactly where ``Origin`` is not;
*   it is set by the browser, not the page. It is a forbidden header name, so
    ``fetch``/``EventSource`` cannot attach or override it -- a page on
    ``evil.example`` gets ``cross-site``, and the only client that can write
    ``same-origin`` is not a browser at all, which is a different threat with a
    different answer (it would also have to forge the token or a ticket).

So the split is by method, and it is a split about what each method *can* do:

*   **State-changing (every POST)** -- require an exact ``Origin``. Browsers
    send it, so this costs the real page nothing, and a forger that suppresses
    it is refused.
*   **Read-only (the two GETs)** -- if an ``Origin`` is present it must match
    exactly, with no fallback; otherwise ``Sec-Fetch-Site: same-origin`` is
    required. So a cross-origin GET carrying an attacker ``Origin`` is refused,
    and a request with neither header is refused: the check only stops being a
    refusal for a request the browser itself vouched for.

Neither branch is a relaxation of the token or the ticket. ``Sec-Fetch-Site``
decides "was this the page I served", which is a question about the *request*;
the token and the ticket decide "is this caller allowed to do this", which is a
question about the *capability*. Both still have to pass.

LAN mode
--------

``forge workbench --lan`` puts the same surface on one named LAN address so it
can be opened on a tablet. The mechanisms above are reused rather than
replaced, which is possible only because of one fact worth stating plainly:

**Off loopback, the UI token stops being an authentication boundary.**
:func:`forge.ui.server.create_app` serves ``GET /`` unauthenticated and
substitutes the token into the page -- that route *is* the token's delivery
mechanism, and requiring the token there would be circular. A device on the
same network can therefore fetch the page, read the token out of its meta tag,
and then satisfy both guards. Nothing in the origin check helps: the page
origin is the bind address, so a legitimate LAN request is a legitimate origin.

So LAN mode does not weaken ``check_origin`` or ``check_same_origin`` -- it adds
the three things a LAN peer does not already have:

*   :class:`Passphrase` -- a secret the operator set in the environment, which
    a peer that can only fetch pages does not have. ``GET /`` withholds the
    token until it is presented, so the delivery mechanism stops being a
    self-service.
*   :class:`UnlockStore` -- one in-process session per device, in an ``HttpOnly``
    ``SameSite=Strict`` cookie, plus per-address failure backoff. Nothing is
    written to disk, so a restart revokes every session.
*   :func:`check_host` -- a ``Host`` allowlist, which is the DNS-rebinding
    defence. Without it, a name an attacker controls resolving to the bind
    address makes the victim's browser address this surface under a host that
    is in no allowlist; the refusal must not depend on the attacker's DNS
    staying put.

The scope is deliberately asymmetric. LAN mode is **read-only by default**:
every GET works, so the tablet is a real observer of a real run, and every
POST -- including ``POST /api/approvals/{id}`` -- is refused until the operator
also passes ``--lan-steer``. The thing being bought down is a guest device
approving a destructive command on this machine, and that failure is
unrecoverable once spent. ``--lan-steer`` widens to parity through the
*identical* route and the identical single-spend channel; it adds no second
approval path and changes nothing in :mod:`forge.tools.approval`.

Two things this mode does not do, so they are not mistaken for oversights: the
transport is cleartext HTTP, so the passphrase and every event frame are
readable by anything that can sniff the network (TLS is a follow-up, not a
default), and :data:`MIN_PASSPHRASE_CHARS` is a floor rather than a
substitute for entropy.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "UiToken",
    "StreamTicketStore",
    "LanConfig",
    "Passphrase",
    "UnlockStore",
    "check_host",
    "check_origin",
    "check_same_origin",
    "allowed_origins",
    "validate_lan_address",
    "InvalidLanAddress",
    "LAN_PASSPHRASE_ENV",
    "MIN_PASSPHRASE_CHARS",
    "SESSION_COOKIE",
    "TOKEN_HEADER",
    "SEC_FETCH_SITE_HEADER",
    "TICKET_TTL_SECONDS",
    "TOKEN_BYTES",
]

#: Header carrying the UI token. Not ``Authorization``: this is not a bearer
#: credential for a resource, it is a same-origin marker for a local process,
#: and naming it as a token keeps it out of the credential-handling habits that
#: would log it.
TOKEN_HEADER = "X-Forge-Token"

#: Fetch Metadata request header. Sent by the browser on *every* request,
#: including the same-origin GETs that carry no ``Origin`` -- which is the
#: entire reason it is consulted. A forbidden header name: page JavaScript
#: cannot set or override it through ``fetch`` or ``EventSource``.
SEC_FETCH_SITE_HEADER = "Sec-Fetch-Site"

#: Bytes of entropy in the token and in each stream ticket. 32 bytes is far
#: beyond guessing and cheap to generate; the value is per-process, so the
#: threat is a page on the same machine guessing it, not an offline attack.
TOKEN_BYTES = 32

#: How long a stream ticket stays usable. Short by design: it exists to carry a
#: single ``EventSource`` handshake from the page script to the stream, not to
#: be a session credential.
TICKET_TTL_SECONDS = 30.0

#: Environment variable holding the LAN passphrase. An environment variable
#: rather than a flag because a flag value lands in shell history and in ``ps``
#: on a machine that is, by definition in this mode, reachable from the network
#: it is the credential for.
LAN_PASSPHRASE_ENV = "FORGE_WORKBENCH_LAN_PASSPHRASE"

#: Floor on passphrase length, enforced by refusing to start rather than by
#: warning. A LAN mode whose secret is ``forge`` protects nothing, and starting
#: anyway would report LAN mode as enabled in the terminal.
MIN_PASSPHRASE_CHARS = 16

#: Cookie carrying an unlocked device's session. ``HttpOnly`` so page
#: JavaScript cannot read it, and ``SameSite=Strict`` so a page on another site
#: does not ride it. Not ``Secure``: there is no TLS in this strike, and a
#: ``Secure`` cookie over ``http://`` is a cookie the browser never sends.
SESSION_COOKIE = "forge_lan_session"

#: How long an unlocked device keeps its session. Long enough to be usable on a
#: tablet that is picked up and put down, short enough that a forgotten
#: unlocked browser is not a standing capability. Process-lifetime anyway --
#: a restart mints a new session id and revokes every one of these.
SESSION_TTL_SECONDS = 12 * 60 * 60.0

#: Failures from one client address before the backoff starts. Small, because
#: the secret is long; this exists to make an offline sweep slow, not to make
#: it impossible.
UNLOCK_FAILURES_BEFORE_BACKOFF = 5

#: First backoff window, doubling per further failure to a cap. Doubling
#: rather than a flat lockout so one clumsy tablet does not lock the operator
#: out for the whole session.
UNLOCK_BACKOFF_BASE_SECONDS = 30.0
UNLOCK_BACKOFF_MAX_SECONDS = 900.0


class InvalidLanAddress(ValueError):
    """A ``--lan-address`` that must not be bound.

    A distinct exception so the CLI can print one actionable line naming the
    reason, rather than a traceback or a bind failure from deep inside uvicorn.
    """


def validate_lan_address(candidate: str | None) -> str:
    """Return ``candidate`` as a canonical literal address, or raise.

    The one thing this rules out is a wildcard, and the reason is a workstation
    commonly has a docker bridge, a VPN tunnel, and a NIC on a routable network
    at the same time: ``0.0.0.0`` would listen on all of them. Naming one
    address means the operator states which network is trusted, and being wrong
    is "the tablet cannot connect" rather than "the surface is on the uplink".

    Loopback is refused too, and not merely because it is redundant: accepting
    it would let ``--lan-address 127.0.0.1`` restate the default in a config
    file and read as a deliberate widening. Link-local and multicast are refused
    because neither is a network a tablet is browsing.

    The result is the parsed address, not the input string, so ``0.0.0.1`` and
    a differently written form of the same address cannot produce two different
    entries in the origin set.
    """
    if candidate is None or not candidate.strip():
        raise InvalidLanAddress(
            "no address given; --lan requires --lan-address with the one "
            "interface to bind, e.g. --lan-address 192.168.1.20"
        )
    try:
        address = ipaddress.ip_address(candidate.strip())
    except ValueError as exc:
        raise InvalidLanAddress(
            f"{candidate!r} is not an IP address; give one literal address, "
            f"not a hostname and not a range"
        ) from exc

    if address.is_unspecified:
        raise InvalidLanAddress(
            f"{address} would listen on every interface; name the one network "
            f"you trust instead"
        )
    if address.is_loopback:
        raise InvalidLanAddress(
            f"{address} is loopback, which is what the workbench already binds "
            f"without any LAN flag"
        )
    if address.is_link_local:
        raise InvalidLanAddress(
            f"{address} is link-local (169.254.0.0/16 or fe80::/10), not a "
            f"network a tablet is browsing"
        )
    if address.is_multicast:
        raise InvalidLanAddress(f"{address} is a multicast address, not a host")
    return str(address)


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


def allowed_origins(port: int, lan_address: str | None = None) -> frozenset[str]:
    """The exact origins this process answers for.

    Both loopback spellings, because ``127.0.0.1`` and ``localhost`` are
    different origins to a browser and an operator may type either. Neither is
    a wildcard: there is no entry that would match a request from anywhere
    else.

    ``lan_address`` adds exactly one entry, in LAN mode, and only because the
    page's own origin on that network *is* that address: a tablet loading
    ``http://192.168.1.20:8765/`` sends ``Origin: http://192.168.1.20:8765`` on
    its POSTs, so refusing it would refuse the legitimate page rather than an
    attacker. It is a single address, never a netmask match and never a
    prefix -- "any host on 192.168.1.0/24" is not expressible here, which is
    deliberate, because the origin a browser reports is the one it was
    *served from* and every additional spelling is a spelling a second device
    could be talked into using.

    The loopback pair stays in the set in LAN mode, so a browser on the
    machine itself keeps working while a tablet is connected.
    """
    origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }
    if lan_address:
        origins.add(f"http://{lan_address}:{port}")
    return frozenset(origins)


def check_origin(origin: str | None, expected: Iterable[str]) -> bool:
    """Whether ``origin`` is exactly one of ``expected``.

    The state-changing check. A missing header is a refusal: a browser always
    sends ``Origin`` on a POST, so absence is not the shape of a legitimate
    request. See the module docstring.
    """
    if not origin:
        return False
    return origin in frozenset(expected)


def check_same_origin(
    origin: str | None, sec_fetch_site: str | None, expected: Iterable[str]
) -> bool:
    """Whether a read-only request came from the page Forge served.

    Two signals, because one is not always there:

    *   An exact ``Origin`` match, when the browser sent one. A cross-origin
        ``GET`` from ``evil.example`` arrives carrying that attacker origin, so
        it is refused here on the same exact-match rule as a POST.
    *   ``Sec-Fetch-Site: same-origin`` when ``Origin`` is **absent**, which is
        what a same-origin ``fetch`` GET and an ``EventSource`` GET actually
        look like. The browser sets it and page JavaScript cannot forge it.

    A *present but wrong* ``Origin`` is refused outright and never falls
    through to the second signal. Both headers are ordinary strings to a
    non-browser client, so a client that can write both can satisfy either
    one; the rule that makes the pair worth anything is that disagreeing with
    an explicit ``Origin`` ends the question. A browser never produces that
    combination -- it sends ``Sec-Fetch-Site: cross-site`` alongside a foreign
    ``Origin`` -- so refusing it costs the real page nothing.

    Both absent is a refusal, so this fails closed against a forger who strips
    headers rather than forging them.
    """
    if origin:
        return check_origin(origin, expected)
    return sec_fetch_site == "same-origin"


def check_host(host: str | None, expected: Iterable[str]) -> bool:
    """Whether the ``Host`` header names an address this process was bound to.

    The DNS-rebinding defence, and the reason it exists in LAN mode and not on
    loopback. An attacker-controlled name that resolves to the bind address
    makes the victim's browser address this surface as
    ``http://forge.attacker.example:8765``; the ``Origin`` that page then sends
    is that same attacker name, which is in no allowlist, so the origin check
    refuses it. That is the right answer, but it is the *attacker's DNS* that
    produced it, and the refusal must not depend on the attacker's DNS staying
    pointed at this host: a rebind that re-points it and re-fetches is a
    different request that can arrive with no attacker origin at all.

    A ``Host`` allowlist is what makes the answer the process's own rather than
    the resolver's. A missing header is a refusal, on the same reasoning as a
    missing ``Origin``: HTTP/1.1 requires it, so its absence is not the shape of
    a real request, and ``uvicorn`` will have already rejected one.

    A ``Host`` header is forgeable by a non-browser client, so this is defence
    in depth and not the boundary. The client that forges it still has to
    present the session cookie or the token, and it still lands on the
    passphrase check.
    """
    if not host:
        return False
    return host.strip() in frozenset(expected)


class Passphrase:
    """The operator's LAN secret: an environment value, never a flag value.

    Read once from the environment, held as a ``str`` in memory only, and
    compared with :func:`hmac.compare_digest` so a caller cannot learn it one
    character at a time by timing. Its :meth:`__repr__` is redacted for the same
    reason :class:`UiToken`'s is: a secret in a traceback is a secret in a file
    someone will later paste into an issue.

    Constructed only from a value the caller has already validated as present
    and long enough -- :meth:`from_environ` is the only honest way to build one,
    and it raises rather than defaulting, because a LAN mode that started with
    an unset or short passphrase would be a network-reachable surface reporting
    itself as protected in the terminal that started it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @classmethod
    def from_environ(cls, environ: dict[str, str]) -> "Passphrase":
        """Build from ``LAN_PASSPHRASE_ENV``, or explain why it cannot be.

        Raises :class:`ValueError` for all three refusals -- unset, shorter than
        :data:`MIN_PASSPHRASE_CHARS`, or whitespace-only -- so the caller
        prints one line and exits. The value is not echoed in any of those
        messages, because a refusal that quoted the too-short secret would put
        it in the scrollback the operator is about to read.
        """
        raw = environ.get(LAN_PASSPHRASE_ENV)
        if raw is None or not raw:
            raise ValueError(
                f"LAN mode needs {LAN_PASSPHRASE_ENV} set to a passphrase of at "
                f"least {MIN_PASSPHRASE_CHARS} characters. Without it a device "
                f"on this network could read the page and its token."
            )
        if len(raw.strip()) < MIN_PASSPHRASE_CHARS:
            raise ValueError(
                f"{LAN_PASSPHRASE_ENV} is shorter than the {MIN_PASSPHRASE_CHARS} "
                f"character minimum; a short passphrase in LAN mode is not "
                f"authentication."
            )
        return cls(raw.strip())

    def matches(self, candidate: str | None) -> bool:
        """Whether ``candidate`` is this passphrase, in constant time."""
        if not candidate:
            return False
        try:
            return hmac.compare_digest(candidate, self._value)
        except (TypeError, UnicodeEncodeError):
            return False

    def __repr__(self) -> str:  # pragma: no cover - operator convenience
        return "<Passphrase redacted>"


class UnlockStore:
    """In-process device sessions and per-address failure backoff.

    One entry per unlocked device, each a random id mapped to an expiry, and
    one failure counter per client address. Both live in memory and die with
    the process, which is the whole persistence story: there is no session file,
    no database, and nothing to clean up, so a restart is a clean revocation of
    every unlocked device.

    The backoff is doubling per failure past
    :data:`UNLOCK_FAILURES_BEFORE_BACKOFF`, capped at
    :data:`UNLOCK_BACKOFF_MAX_SECONDS`. A flat lockout would be a denial of
    service any neighbour on the network could trigger against the one device
    the operator is trying to use; doubling is enough to make a sweep slow.

    Lockout does **not** exempt a correct passphrase. That is deliberate: if a
    correct answer were served while a window was open, the window would be an
    oracle for whether a guess was right, and the backoff would protect nothing.
    So a client in a backoff window is refused before the passphrase is even
    compared.

    The window is time-based rather than a flat counter, and that is the
    difference between this class existing and not existing: a counter alone
    locks a client out until the process restarts, which is a denial of service
    any neighbour on the network could trigger against the one device the
    operator is trying to use. Each failure past
    :data:`UNLOCK_FAILURES_BEFORE_BACKOFF` pushes the window to
    ``now + base * 2 ** (failures - threshold)``, capped at ``maximum``, and it
    decays back to the threshold as the windows pass.
    """

    def __init__(
        self,
        *,
        ttl: float = SESSION_TTL_SECONDS,
        threshold: int = UNLOCK_FAILURES_BEFORE_BACKOFF,
        base: float = UNLOCK_BACKOFF_BASE_SECONDS,
        maximum: float = UNLOCK_BACKOFF_MAX_SECONDS,
    ) -> None:
        self._ttl = ttl
        self._threshold = threshold
        self._base = base
        self._max = maximum
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, float] = {}
        self._counts: dict[str, int] = {}

    # -- sessions -------------------------------------------------------

    def open_session(self) -> str:
        """Mint a session id. Returns the value the client must present."""
        session = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._purge_locked()
            self._sessions[session] = time.monotonic() + self._ttl
        return session

    def is_unlocked(self, session: str | None) -> bool:
        """Whether ``session`` is a live session of this process.

        A refresh re-derives everything from the server, so this is consulted
        on each request rather than trusted from a page. The entry is consumed
        on the check only by expiry: a session is not single-use, because the
        page makes a status read, a ticket, and a stream per refresh and
        revoking it between them would break a working tablet.
        """
        if not session:
            return False
        now = time.monotonic()
        with self._lock:
            expiry = self._sessions.get(session)
            if expiry is None:
                return False
            if now > expiry:
                del self._sessions[session]
                return False
            return True

    # -- backoff --------------------------------------------------------

    def locked_out(self, client: str | None) -> bool:
        """Whether ``client`` is inside a backoff window.

        Reads only the counter and the deadline, so it can be called *before*
        the passphrase is compared -- which is what makes the correct answer
        refusing itself possible. An expired window forgets the failure, so a
        device that waited the window out is served again.
        """
        if not client:
            return False
        now = time.monotonic()
        with self._lock:
            count = self._counts.get(client, 0)
            if count < self._threshold:
                return False
            deadline = self._failures.get(client, 0.0)
            if now >= deadline:
                # The window closed. Drop back to the threshold rather than to
                # zero, so a client that keeps guessing is still slowed down.
                self._counts[client] = self._threshold - 1
                self._failures.pop(client, None)
                return False
            return True

    def record_failure(self, client: str | None) -> None:
        """Count one failed unlock from ``client`` and set its window."""
        if not client:
            return
        now = time.monotonic()
        with self._lock:
            count = self._counts.get(client, 0) + 1
            self._counts[client] = count
            if count < self._threshold:
                return
            steps = min(count - self._threshold, 20)
            window = min(self._base * (2**steps), self._max)
            # Never shorten a window that is already running: a further failure
            # from the same client must not buy back time.
            self._failures[client] = max(
                self._failures.get(client, 0.0), now + window
            )

    def record_success(self, client: str | None) -> None:
        """Clear ``client``'s counter after a correct passphrase.

        Without this a single early typo would carry its backoff into the rest
        of the session, so a device that fumbled its way in would still be
        slowed down on every later mistake.
        """
        if not client:
            return
        with self._lock:
            self._counts.pop(client, None)
            self._failures.pop(client, None)

    @property
    def failure_count(self) -> dict[str, int]:
        """Per-address failure counts. Exposed for tests and diagnostics."""
        with self._lock:
            return dict(self._counts)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        for key in [k for k, expiry in self._sessions.items() if now > expiry]:
            del self._sessions[key]


@dataclass(frozen=True)
class LanConfig:
    """LAN mode as the server sees it. Absent means loopback, unchanged.

    Frozen because every field here is a security decision made once by the
    operator and then read on every request; a mutable one would let a
    capability be widened after the terminal said the surface was read-only.
    """

    #: The one literal address bound and served. Already canonical: it passed
    #: :func:`validate_lan_address`.
    address: str

    #: Whether a LAN client may POST. ``False`` is the default posture -- every
    #: GET works, and every write including ``POST /api/approvals/{id}`` is
    #: refused.
    steer: bool = False



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