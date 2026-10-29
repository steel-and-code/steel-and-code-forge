"""The Jira Board: a read-only projection of the Steel and Code FORGE workflow.

This module is the whole of the Workbench's Jira behaviour, in the same way
``forge.ui.models`` is the whole of its model-health behaviour: the routes
validate nothing, decide nothing, and hold no vocabulary of their own.

It does not touch the local Order Board. ``forge.ui.orders`` keeps its own
six-state blacksmith vocabulary and its own ``.forge/orders.json``, and nothing
here reads or writes that file. That separation is not caution for its own
sake: ``OrderStore`` refuses a stored order whose ``status`` is outside
``STATUSES``, so renaming or extending that vocabulary would make every
existing board raise ``OrderStoreError`` and the surface answer 500 forever.
The only way to keep real local work orders is to not put them in the same
list as Jira issues.

Board identity. A column's stable key is prefixed and never a bare word::

    status = "jira:in-engineering"   # stable identifier, travels over the wire
    label  = "In Engineering"        # the operator's wording, from Jira
    id     = "10006"                 # the Jira status id, recorded for audit

The ``jira:`` prefix is what makes "distinguish Jira-sourced issues from local
work orders" structural rather than a caption a later edit could delete: a
prefixed key cannot collide with ``orders_received``, so the two sets can be
rendered side by side and cannot be merged by accident.

Matching is by status name, not by id. ``JiraIssue`` carries
``fields.status.name`` and no status id, and Jira Cloud's board-columns
endpoint is not reachable with the read-only credential this project has, so
the id is what this board *records*, not what it filters on. ``COLUMNS`` names
both, so a rename on the Jira side is one line here rather than a silent gap.

Read-only by absence. There is no move, edit, create, or delete function in
this module, and no import of the dispatcher, the session, or a gateway. A
card here cannot start an agent run, cannot spend a model call, and cannot
write to Jira, because nothing here is able to. Making it writable later is an
explicit change to this file plus a reviewed route, not a flag.

Every failure is a reportable value. No configuration, no credentials, a
refused request, and a genuinely empty board are four different outcomes with
four different codes, and none of them is an empty board. Publishing an empty
board because the token was never set would tell an operator their engineering
pipeline is empty when the truth is that Forge cannot ask. A failed refresh
also replaces nothing: the last projected board stays in the cache.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "COLUMNS",
    "JiraColumn",
    "JiraIssueCard",
    "JiraBoard",
    "JiraBoardError",
    "NOT_CONFIGURED",
    "NO_CREDENTIALS",
    "FETCH_FAILED",
    "DEFAULT_PROJECT_KEY",
    "PROJECT_KEY_ENV",
    "OVERFLOW_KEY",
    "MAX_BOARD_ISSUES",
    "project_issues",
    "project_key_for",
    "load_jira_issues",
]


@dataclass(frozen=True)
class JiraColumn:
    """One board column: a stable key, the operator's wording, its Jira id.

    ``id`` is the status id this column corresponds to. It travels to the page
    for audit and is never a filter, because the issue records Forge reads carry
    a status *name* and no id.
    """

    key: str
    label: str
    name: str
    id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "status": self.key,
            "label": self.label,
            "jira_status": self.name,
            "jira_status_id": self.id,
        }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _column(id_: str, name: str) -> JiraColumn:
    return JiraColumn(key=f"jira:{_slug(name)}", label=name, name=name, id=id_)


#: The seven workflow states, in board order, with the Jira status ids they
#: correspond to on the FORGE / KAN board (10004-10010).
#:
#: This is the order an operator reads the engineering pipeline in, so it is a
#: tuple and the projection publishes it verbatim rather than sorting a set. The
#: keys are derived from the names here rather than written beside them: a key
#: and a name that disagree is a column whose cards no filter can find.
COLUMNS: tuple[JiraColumn, ...] = (
    _column("10004", "Backlog"),
    _column("10005", "Ready for Forge"),
    _column("10006", "In Engineering"),
    _column("10007", "Needs Architect Input"),
    _column("10008", "In Review"),
    _column("10009", "Approved"),
    _column("10010", "Released"),
)

#: The seven keys, in order. Published so the page renders the backend's
#: vocabulary instead of keeping a second copy that can drift from it.
COLUMN_KEYS: tuple[str, ...] = tuple(column.key for column in COLUMNS)

#: Column key of the catch-all for an issue whose status is none of the seven.
#: Emitted last, and only when it holds something, so a healthy board shows
#: exactly seven columns. A status added on the Jira side appears here as a
#: named column with its cards in it rather than as issues that vanished.
OVERFLOW_KEY = "jira:unmapped"
OVERFLOW_LABEL = "Unmapped status"

#: The Jira project this board projects, and the variable that overrides it. A
#: board belongs to a project, not to a site, so the key is not a constant an
#: operator cannot leave.
DEFAULT_PROJECT_KEY = "FORGE"
PROJECT_KEY_ENV = "FORGE_JIRA_PROJECT"

#: Why a projection failed, as stable strings. The page branches on these rather
#: than on prose, and the server passes them through untouched.
NOT_CONFIGURED = "not_configured"
NO_CREDENTIALS = "no_credentials"
FETCH_FAILED = "fetch_failed"

#: Longest reason sentence published. A provider body can quote the request it
#: rejected, and that request carried the Authorization header, so only a
#: bounded single line ever reaches a response.
_MAX_REASON = 200

#: Ceiling on issues projected from one refresh. The board is a workflow view,
#: not a database dump; past this the columns are unreadable and the page has
#: become a download. Truncation is reported as a value, never silent.
MAX_BOARD_ISSUES = 400


class JiraBoardError(RuntimeError):
    """A code the surface can branch on, and one operator-safe sentence.

    One exception type with a machine-readable ``code`` rather than a class per
    failure, because every consumer does the same thing with any of them: report
    it and keep the previous snapshot. What differs is the code, and that is what
    lets a page tell "not configured" from "Jira said no" without matching prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bounded(value: object) -> str:
    """One short operator-safe line, never a provider body."""
    return " ".join(str(value or "").split())[:_MAX_REASON]


def _now() -> str:
    """UTC, second resolution, matching the model-health stamp's format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def project_key_for(environ: Mapping[str, str] | None = None) -> str:
    """The project key to read, from the environment or the default.

    Blank means unset, so an ``export FORGE_JIRA_PROJECT=`` behaves like an
    absent one rather than as a query for a project named nothing.
    """
    env = os.environ if environ is None else environ
    value = str(env.get(PROJECT_KEY_ENV, "") or "").strip()
    return value or DEFAULT_PROJECT_KEY


@dataclass(frozen=True)
class JiraIssueCard:
    """One issue, as the board draws it.

    Deliberately narrower than ``JiraIssue``: no description, no labels, no
    reporter. A board card is a workflow row, and the fewer fields projected,
    the less a page can render something an operator never meant to publish on a
    surface a tablet can reach.

    ``url`` is the only field that leaves the page, and the renderer validates it
    before it becomes a link -- never a scheme an operator did not choose.
    ``source`` is the provenance badge, and the reason an issue and a local Order
    can share a board without sharing an identity.
    """

    key: str
    title: str
    status: str
    status_label: str
    jira_status: str
    jira_status_id: str
    priority: str
    assignee: str
    url: str
    updated: str
    source: str = "jira"

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "status_label": self.status_label,
            "jira_status": self.jira_status,
            "jira_status_id": self.jira_status_id,
            "priority": self.priority,
            "assignee": self.assignee,
            "url": self.url,
            "updated": self.updated,
            "source": self.source,
        }


def _clean(value: object, fallback: str = "") -> str:
    return " ".join(str(value or "").split()) or fallback


def _column_for(status: object) -> JiraColumn | None:
    """The column an issue belongs to, or ``None`` if it is in none.

    Comparison ignores case and runs of whitespace, because a workflow name is
    typed by a human on the Jira side and ``Ready  for Forge`` is the same column
    as ``Ready for Forge``.
    """
    name = _clean(status).casefold()
    if not name:
        return None
    for column in COLUMNS:
        if column.name.casefold() == name:
            return column
    return None


def _card(issue: Mapping[str, Any], column: JiraColumn | None) -> JiraIssueCard:
    key = _clean(issue.get("key"))
    raw_status = _clean(issue.get("status"))
    if column is not None:
        return JiraIssueCard(
            key=key,
            title=_clean(issue.get("summary"), "Untitled issue"),
            status=column.key,
            status_label=column.label,
            jira_status=column.name,
            jira_status_id=column.id,
            priority=_clean(issue.get("priority")),
            assignee=_clean(issue.get("assignee")),
            url=_clean(issue.get("url")),
            updated=_clean(issue.get("updated")),
        )
    # Unmapped keeps the status the site actually reported as its label, so the
    # card says where the issue really is. Inventing one of the seven for it
    # would move real work into a column it is not in, the more expensive of the
    # two errors.
    return JiraIssueCard(
        key=key,
        title=_clean(issue.get("summary"), "Untitled issue"),
        status=OVERFLOW_KEY,
        status_label=raw_status or OVERFLOW_LABEL,
        jira_status=raw_status,
        jira_status_id=_clean(issue.get("status_id")),
        priority=_clean(issue.get("priority")),
        assignee=_clean(issue.get("assignee")),
        url=_clean(issue.get("url")),
        updated=_clean(issue.get("updated")),
    )


def project_issues(
    issues: Sequence[Mapping[str, Any]],
    *,
    project: str = DEFAULT_PROJECT_KEY,
    max_issues: int = MAX_BOARD_ISSUES,
) -> dict[str, object]:
    """Project issue mappings into the seven columns.

    Takes plain mappings rather than ``JiraIssue`` so the projection is testable
    without a Jira client, and so one function serves a cache row and a live
    fetch alike.

    **One issue appears in exactly one column, once.** Records are de-duplicated
    by ``key`` before grouping and the first sighting wins, so a refresh that
    re-reads a page boundary cannot put the same ticket in two columns or repeat
    it in one. ``unique_keys`` makes that claim checkable rather than assumed: it
    is the number of distinct keys seen, and it must equal ``total``.
    """
    by_key: dict[str, Mapping[str, Any]] = {}
    skipped = 0
    for issue in issues:
        if not isinstance(issue, Mapping):
            skipped += 1
            continue
        key = _clean(issue.get("key"))
        if not key:
            # A record with no key cannot be placed, cannot be de-duplicated, and
            # cannot be linked to. It is counted, not dropped silently.
            skipped += 1
            continue
        # First wins. Choosing the last would make the answer depend on the order
        # a page happened to arrive in, which is not a fact about the issue.
        by_key.setdefault(key, issue)

    ordered = list(by_key.values())
    truncated = len(ordered) > max_issues
    if truncated:
        ordered = ordered[:max_issues]

    grouped: dict[str, list[JiraIssueCard]] = {column.key: [] for column in COLUMNS}
    overflow: list[JiraIssueCard] = []
    for issue in ordered:
        column = _column_for(issue.get("status"))
        card = _card(issue, column)
        if column is None:
            overflow.append(card)
        else:
            grouped[column.key].append(card)

    columns: list[dict[str, object]] = [
        {
            **column.as_dict(),
            "source": "jira",
            "count": len(grouped[column.key]),
            "issues": [card.as_dict() for card in grouped[column.key]],
        }
        for column in COLUMNS
    ]
    if overflow:
        columns.append(
            {
                "key": OVERFLOW_KEY,
                "status": OVERFLOW_KEY,
                "label": OVERFLOW_LABEL,
                "jira_status": "",
                "jira_status_id": "",
                "source": "jira",
                "count": len(overflow),
                "issues": [card.as_dict() for card in overflow],
            }
        )

    return {
        # The board's order is published, never inferred by the page.
        "columns": columns,
        "statuses": [str(column["status"]) for column in columns],
        "total": sum(int(column["count"]) for column in columns),
        "unique_keys": len(by_key),
        "project": project,
        "truncated": truncated,
        "skipped_no_key": skipped,
        "checked_at": _now(),
    }


def load_jira_issues(
    environ: Mapping[str, str] | None = None,
    *,
    workspace_root: object = None,
    max_issues: int = MAX_BOARD_ISSUES,
) -> list[dict[str, Any]]:
    """Read this project's issues from Jira, through the existing client.

    Returns plain mappings, because the projection must not have to know whether
    its input was a live fetch or a cache row.

    **The SQLite cache is deliberately not used.** ``JiraCache`` holds one
    persistent connection, which Python binds to the thread that opened it, and
    the Workbench runs every store call on a worker thread via
    ``asyncio.to_thread`` -- a different thread per call. A cache held in app
    state therefore raises ``sqlite3.ProgrammingError`` on the second request.
    Building a cache per call would work but would also make the board depend on
    a background sync process to have anything to show, so the board reads the
    source and the cache stays where the CLI put it.

    Raises:
        JiraBoardError: with ``not_configured`` when no Jira instance is
            configured, ``no_credentials`` when a URL is configured without the
            matching secret, or ``fetch_failed`` for anything the client reports.
            Never a raw provider message, and never a credential.
    """
    from ..jira.client import JiraClient, JiraClientError
    from ..jira.config import get_default_config

    env = os.environ if environ is None else environ
    config = get_default_config(
        workspace_root if workspace_root is None else workspace_root, env
    )
    if config is None:
        raise JiraBoardError(
            NOT_CONFIGURED,
            "no Jira instance is configured, so there is no board to read; set "
            "FORGE_JIRA_URL, FORGE_JIRA_EMAIL and FORGE_JIRA_API_TOKEN, or "
            "create .forge/jira.toml",
        )
    if not config.has_credentials:
        raise JiraBoardError(
            NO_CREDENTIALS,
            f"Jira instance {config.instance_name!r} has a URL but no email and "
            "API token, so Forge cannot ask it for issues",
        )

    project = project_key_for(env)
    try:
        client = JiraClient(config)
        issues = client.get_project_issues([project], max_issues=max_issues)
    except JiraClientError as exc:
        # The client's own errors are already bounded and credential-free; they
        # still pass through _bounded so one long provider line cannot become a
        # response body.
        raise JiraBoardError(FETCH_FAILED, _bounded(exc)) from None
    except OSError as exc:
        raise JiraBoardError(
            FETCH_FAILED, f"Jira could not be reached ({type(exc).__name__})"
        ) from None
    except Exception as exc:  # noqa: BLE001 - a refresh reports, it does not raise
        # A failure inside the client's own plumbing is still an answer about
        # reachability, not a traceback for the operator to read at the page.
        raise JiraBoardError(
            FETCH_FAILED, f"Jira could not be read ({type(exc).__name__})"
        ) from None

    return [issue.to_dict() for issue in issues]


class JiraBoard:
    """The cached Jira projection, and the one way to replace it.

    Args:
        loader: Returns the issue mappings to project. Injected, so every test
            drives the cache, the failure states, and the de-duplication without
            a Jira client, a socket, or a credential. Defaults to
            :func:`load_jira_issues`.
        environ: Credential and project mapping, handed to the loader and never
            published.
        project: The project key to label the board with. Read from ``environ``
            when not given.

    **A read never reaches Jira.** :meth:`status` returns what is cached, which
    is what makes ``GET /api/jira-board`` an observation. :meth:`refresh` is the
    only path that can open a socket, and the surface makes it a POST for exactly
    that reason.

    **A failed refresh replaces nothing.** The previous projection stays cached
    and the failure is reported beside it, so a transient refusal cannot empty a
    board an operator is reading. The two states are distinct in the payload:
    ``state`` is ``"ok"`` only when the board on screen came from a successful
    read, and ``"stale"`` when it is an older board with a newer failure.
    """

    def __init__(
        self,
        loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        project: str | None = None,
    ) -> None:
        self._environ = dict(os.environ if environ is None else environ)
        self._project = project or project_key_for(self._environ)
        self._loader = loader if loader is not None else self._default_loader
        self._lock = threading.Lock()
        self._board: dict[str, object] | None = None
        self._error: JiraBoardError | None = None
        # Whether anything has ever been projected. Without it the page has a
        # "not loaded yet" state to show; with it, a failure is reported against
        # the board still on screen rather than replacing it.
        self._loaded = False

    def _default_loader(self) -> Sequence[Mapping[str, Any]]:
        return load_jira_issues(self._environ)

    # -- read -----------------------------------------------------------

    def available(self) -> bool:
        """Whether a board has ever been projected successfully."""
        with self._lock:
            return self._loaded

    def status(self) -> dict[str, object]:
        """The cached board, wrapped as thinly as it can be.

        ``state`` is one of ``empty`` (nothing has been projected yet), ``ok``,
        ``stale`` (an older board, a newer failure), or ``error`` (nothing to
        show and a reason). Those four are what lets the page distinguish "the
        pipeline has no work" from "Forge could not ask", which is the
        distinction an operator acts on.
        """
        with self._lock:
            if self._board is None:
                return {
                    "state": "empty" if self._error is None else "error",
                    "columns": [],
                    "statuses": [],
                    "total": 0,
                    "project": self._project,
                    "error": (
                        None
                        if self._error is None
                        else {
                            "code": self._error.code,
                            "reason": _bounded(self._error),
                        }
                    ),
                    "checked_at": None,
                }
            board = dict(self._board)
            board["state"] = "ok" if self._error is None else "stale"
            board["error"] = (
                None
                if self._error is None
                else {"code": self._error.code, "reason": _bounded(self._error)}
            )
            return board

    # -- refresh --------------------------------------------------------

    def refresh(self) -> dict[str, object]:
        """Project the board once, replacing the cache only on success.

        Synchronous and blocking, and bounded by the client's own timeout. The
        caller runs it off the event loop.
        """
        try:
            issues = self._loader()
        except JiraBoardError as error:
            with self._lock:
                self._error = error
            # A failure never replaces the board, so a transient refusal cannot
            # empty a page an operator is reading. status() reports `stale`
            # against the retained board and `error` when there is none.
            return self.status()
        except Exception as exc:  # noqa: BLE001 - a refresh reports, never raises
            with self._lock:
                self._error = JiraBoardError(
                    FETCH_FAILED, f"Jira could not be read ({type(exc).__name__})"
                )
            return self.status()

        board = project_issues(issues, project=self._project)
        with self._lock:
            self._board = board
            self._error = None
            self._loaded = True
        return self.status()

