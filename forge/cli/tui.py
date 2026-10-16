"""Curses front end to the existing Forge CLI agent runners."""

from __future__ import annotations

import curses
import io
import inspect
import os
import queue
import shutil
import subprocess
import sys
import textwrap
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.state import ConversationState, ConversationStateStore

AGENTS = ("Neighbor", "Hermes", "Byte")
_TASK_PREFIX = "Task [typing]: "


@dataclass(frozen=True)
class SubmittedTask:
    selected_agent: str
    task_text: str
    workspace: Path
    conversation_state: ConversationState | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ExecutionEvent:
    """A worker result delivered to the curses thread."""

    kind: str
    value: Any = None


class _ResponseStream(io.StringIO):
    """Keep the CLI display intact while retaining its final response print.

    Both native and external runners print the final response last, in a single
    ``print(result.text)`` call. Earlier prints contain reports, not the answer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.response = ""
        self._at_print_boundary = True

    def write(self, text: str) -> int:
        if text != "\n" or self._at_print_boundary:
            self.response = text
            self._at_print_boundary = False
        else:
            self._at_print_boundary = True
        return super().write(text)


class MainThreadApprovalChannel:
    """Approval channel safe for a worker: prompting is owned by the UI loop.

    ``present`` never touches curses or stdin.  It publishes a request and waits
    for the main thread to call :meth:`answer`; unanswered requests fail closed.
    """

    def __init__(self) -> None:
        from ..tools.approval import TerminalApprovalChannel
        self._requests: queue.Queue[tuple[object, str, threading.Event, list[str | None]]] = queue.Queue()

    @property
    def available(self) -> bool:
        return True

    def present(self, request: object, prompt: str) -> str | None:
        done = threading.Event()
        answer: list[str | None] = [None]
        self._requests.put((request, prompt, done, answer))
        done.wait()
        return answer[0]

    def next_request(self) -> tuple[object, str, threading.Event, list[str | None]] | None:
        try:
            return self._requests.get_nowait()
        except queue.Empty:
            return None

    @staticmethod
    def answer(item: tuple[object, str, threading.Event, list[str | None]], value: str | None) -> None:
        _, _, done, answer = item
        answer[0] = value
        done.set()


class ExecutionWorker:
    """Run the existing CLI in a thread and publish only data events."""

    def __init__(self, submitted: SubmittedTask) -> None:
        self.submitted = submitted
        self.events: queue.Queue[ExecutionEvent] = queue.Queue()
        self.approvals = MainThreadApprovalChannel()
        self.thread = threading.Thread(target=self._run, name="forge-tui-worker", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            try:
                result = _execute(self.submitted, channel=self.approvals)
            except TypeError as exc:
                # Keep small test/application adapters that implement the old
                # callable shape usable while the channel-aware path rolls out.
                if "channel" not in str(exc):
                    raise
                result = _execute(self.submitted)
            self.events.put(ExecutionEvent("complete", result))
        except Exception as exc:  # worker errors are data, never UI exceptions
            self.events.put(ExecutionEvent("error", exc))


class _TaskEditor:
    """Small curses-independent text editor used by the task field."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.cursor = len(text)

    def insert(self, value: str) -> None:
        self.text = self.text[:self.cursor] + value + self.text[self.cursor:]
        self.cursor += len(value)

    def backspace(self) -> None:
        if self.cursor:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1

    def move(self, key: int, width: int) -> None:
        """Move by character, or by visual line for Up/Down."""
        if key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in (curses.KEY_HOME, curses.KEY_END):
            lines = _task_layout(self.text, width, _TASK_PREFIX)
            line = next((line for line in lines if line[0] <= self.cursor <= line[1]), lines[-1])
            self.cursor = line[0] if key == curses.KEY_HOME else line[1]
        elif key in (curses.KEY_UP, curses.KEY_DOWN):
            lines = _task_layout(self.text, width, _TASK_PREFIX)
            current = next((i for i, line in enumerate(lines) if line[0] <= self.cursor <= line[1]), len(lines) - 1)
            target = max(0, current - 1) if key == curses.KEY_UP else min(len(lines) - 1, current + 1)
            column = self.cursor - lines[current][0]
            self.cursor = min(lines[target][1], lines[target][0] + column)


def _task_layout(text: str, width: int, prefix: str = _TASK_PREFIX) -> list[tuple[int, int, str]]:
    """Return (text start, text end, displayed text) for wrapped task lines."""
    first_width = max(1, width - len(prefix) - 1)
    # Continuation rows carry the same visual indent as the first row, so their
    # text width must account for that indent too.  Otherwise addnstr clips the
    # tail of every wrapped continuation line.
    other_width = max(1, width - len(prefix) - 1)
    result: list[tuple[int, int, str]] = []
    start = 0
    line_width = first_width
    for part in text.split("\n"):
        if not part:
            result.append((start, start, ""))
        else:
            while part:
                chunk = part[:line_width]
                result.append((start, start + len(chunk), chunk))
                start += len(chunk)
                part = part[len(chunk):]
                line_width = other_width
        start += 1  # account for the newline between logical paragraphs
        line_width = other_width
    if not result:
        result.append((0, 0, ""))
    # The split accounting above adds one past the real end; correct the final line.
    if text and not text.endswith("\n"):
        result[-1] = (result[-1][0], len(text), result[-1][2])
    return result


def _execute(submitted: SubmittedTask, *, channel=None) -> tuple[int, str, str, str]:
    """Run the existing CLI route.

    The synchronous path retains the established terminal approval fallback;
    asynchronous TUI runs pass ``MainThreadApprovalChannel`` so the worker never
    reads stdin or invokes curses.
    """
    from ..tools.approval import TerminalApprovalChannel
    from .main import main

    out, err = _ResponseStream(), io.StringIO()
    # Run against a candidate transcript: failed or interrupted attempts must
    # not leave an orphaned user message or an unanswered tool call in history.
    saved = submitted.conversation_state
    candidate = (ConversationState(list(saved.messages), saved.turns, dict(saved.denials))
                 if saved is not None and submitted.selected_agent.lower() in ("byte", "neighbor")
                 else None)
    suspended = False

    def suspend() -> None:
        nonlocal suspended
        if not suspended:
            curses.endwin()
            suspended = True

    def restore() -> None:
        nonlocal suspended
        if suspended:
            curses.reset_prog_mode()
            suspended = False

    if channel is None:
        # A worker's stdout writes bypass curses and can reappear as stale text
        # after the result frame. The TUI itself owns the running indicator.
        print(f"Running {submitted.selected_agent}...", file=sys.stdout, flush=True)
        channel = TerminalApprovalChannel(
            prompt_stream=sys.stdout, before_prompt=suspend, after_prompt=restore,
        )
    try:
        kwargs = {"out": out, "err": err, "channel": channel}
        if candidate is not None:
            kwargs["conversation_state"] = candidate
        code = main(
            [submitted.selected_agent.lower(), "--workspace", str(submitted.workspace), submitted.task_text],
            **kwargs,
        )
    finally:
        # Restore even if the approval prompt or runner fails after suspending.
        restore()
    if code == 0 and saved is not None and candidate is not None:
        saved.messages[:] = candidate.messages
        saved.turns = candidate.turns
        saved.denials.clear()
        saved.denials.update(candidate.denials)
    return code, out.getvalue(), err.getvalue(), out.response if code == 0 else ""

def _confirm_engineering(stdscr: curses.window, workspace: Path) -> bool:
    """Require a deliberate yes after displaying the external authority boundary."""
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    lines = (
        "Open Hermes Engineering Session?",
        "Hermes uses its own native tools, credentials, and approval policy.",
        "Forge's workspace does NOT confine it; Forge's per-tool security",
        "controls do NOT apply to Hermes's internal tool calls.",
        f"Starting directory: {workspace}",
        "Press Y to open Hermes; any other key cancels.",
    )
    row = 0
    for line in lines:
        for part in textwrap.wrap(line, width=max(1, width - 2), break_long_words=True) or [""]:
            _put(stdscr, row, 1, part, max(1, width - 2))
            row += 1
    stdscr.refresh()
    return row < height and stdscr.getch() == ord("Y")

def _open_hermes_engineering(workspace: Path) -> int:
    """Hand the foreground terminal to Hermes; never use the text-only runner."""
    from ..external.hermes import PASSTHROUGH_ENV

    program = shutil.which("hermes")
    if program is None:
        raise FileNotFoundError("Hermes is not installed on PATH")
    env = {key: os.environ[key] for key in PASSTHROUGH_ENV if key in os.environ}
    env.setdefault("HOME", os.path.expanduser("~"))
    env["FORGE_INVOKED_HERMES"] = "1"
    # --cli avoids nesting Hermes's own curses-like TUI. No query is submitted
    # here: the operator drives the interactive session and its native approvals.
    curses.endwin()
    try:
        return subprocess.run(
            [program, "chat", "--cli", "--in", str(workspace)],
            cwd=workspace, env=env, check=False,
        ).returncode
    finally:
        curses.reset_prog_mode()


def _copy_response(response: str) -> str:
    """Copy the complete answer via an installed system clipboard utility."""
    candidates = (["wl-copy"], ["xclip", "-selection", "clipboard"],
                  ["xsel", "--clipboard", "--input"], ["pbcopy"])
    # Prefer the native Wayland clipboard when running under Wayland, but still
    # allow it as a fallback if the display environment is not advertised.
    if not os.environ.get("WAYLAND_DISPLAY"):
        candidates = (candidates[1], candidates[2], candidates[0], candidates[3])
    for command in candidates:
        program = shutil.which(command[0])
        if program is None:
            continue
        try:
            result = subprocess.run(
                [program, *command[1:]], input=response, text=True, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return "Copied response to clipboard"
    return "Error: clipboard unavailable or copy failed"


def _export_response(workspace: Path, response: str) -> Path:
    """Create a Markdown answer in the workspace, never replacing an entry."""
    for index in range(1000):
        name = "forge-response.md" if index == 0 else f"forge-response-{index}.md"
        path = workspace / name
        try:
            with path.open("x", encoding="utf-8") as output:
                output.write(response)
            return path
        except FileExistsError:
            continue
    raise FileExistsError("No unused response filename available")


def _output_layout(output: str, width: int) -> list[str]:
    """Wrap output for the current terminal width without losing its shape."""
    width = max(1, width - 1)
    wrapped: list[str] = []
    for line in output.split("\n"):
        if not line:
            wrapped.append("")
            continue
        # Do not strip whitespace: leading indentation is meaningful in agent
        # answers (and in code blocks).  textwrap only breaks the display copy;
        # the original response remains stored in ``output``.
        chunks = textwrap.wrap(line, width=width, replace_whitespace=False,
                               drop_whitespace=False, break_long_words=True,
                               break_on_hyphens=False)
        wrapped.extend(chunks or [""])
    return wrapped


def _output_view(total: int, scroll: int, available: int) -> tuple[int, int, int]:
    """Clamp output scroll and return (start, stop, clamped_scroll)."""
    available = max(1, available)
    maximum = max(0, total - available)
    scroll = min(max(0, scroll), maximum)
    return scroll, min(total, scroll + available), scroll


def _task_view(layout: list[tuple[int, int, str]], cursor: int, available: int) -> tuple[int, int]:
    """Return the visible line range, keeping the editor cursor on screen."""
    if not layout or available <= 0:
        return 0, 0
    available = max(1, available)
    cursor_line = next(
        (i for i, (start, end, _) in enumerate(layout) if start <= cursor <= end),
        len(layout) - 1,
    )
    if len(layout) <= available:
        return 0, len(layout)
    first = min(max(0, cursor_line - available + 1), len(layout) - available)
    return first, first + available


def _abbreviated_workspace(workspace: Path, available: int) -> str:
    """Fit a workspace path without hiding its useful trailing component."""
    value = str(workspace)
    if available <= 0:
        return ""
    if len(value) <= available:
        return value
    if available <= 3:
        return "." * available
    suffix = value.rsplit("/", 1)[-1] or value
    if available <= len(suffix) + 2:
        return "…" + suffix[-(available - 1):]
    return "…/" + suffix[-(available - 2):]


def _layout(height: int, width: int, task_lines: int = 1) -> dict[str, int]:
    """Calculate the chat-first geometry without touching curses.

    Keeping geometry separate makes resize behaviour deterministic and prevents
    the composer from drifting back toward the top when a transcript grows.
    """
    footer = max(0, height - 1)
    panel = 24 if width >= 100 else 0
    composer = min(max(4, task_lines + 3), max(4, height - 4))
    composer_top = max(2, footer - composer)
    return {"header": 0, "chat_top": 2, "chat_bottom": composer_top,
            "composer_top": composer_top, "composer_bottom": footer,
            "footer": footer, "panel": panel, "chat_width": max(1, width - panel)}


def _put(stdscr: curses.window, row: int, col: int, text: str, width: int) -> None:
    """Best-effort bounded write for terminals too small to host a region."""
    height, terminal_width = stdscr.getmaxyx()
    if not (0 <= row < height and 0 <= col < terminal_width - 1 and width > 0):
        return
    try:
        stdscr.addnstr(row, col, text, min(width, terminal_width - col - 1))
    except curses.error:
        # Some curses implementations reject a write ending at the edge even
        # after the conservative bound above.  A clipped frame is preferable
        # to losing the whole TUI on resize.
        return


def _render(stdscr: curses.window, workspace: Path, highlighted: int, selected_agent: str,
            task_text: str, editing_task: bool, submitted: SubmittedTask | None,
            status: str = "", output: str = "", editor: _TaskEditor | None = None,
            output_scroll: int = 0, activity: list[str] | None = None) -> int:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    # Colour is an enhancement only; terminals without colour (and test doubles)
    # continue through the same monochrome drawing path.
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            stdscr.attrset(curses.color_pair(1))
    except (curses.error, AttributeError):
        pass
    model = __import__("os").environ.get("FORGE_MODEL") or "default"
    prefix = _TASK_PREFIX if editing_task else "Task [Tab to type]: "
    task_layout = _task_layout(task_text, max(1, width - 1), prefix)
    geo = _layout(height, width, len(task_layout))
    chat_width = max(1, geo["chat_width"] - 1)

    # A compact, single-line header deliberately carries all persistent context;
    # agent controls remain available through the existing keys, not in chat.
    workspace_text = _abbreviated_workspace(workspace, max(1, width - 1))
    context = (f"Forge | Agent: {selected_agent} | Model: {model} | Workspace: "
               f"{workspace_text}")
    # A long workspace must not hide the result of an execution.  The agent
    # remains visible on the controls row if a long error fills this header.
    header = f"{status} | {context}" if status else f"{context} | Status: Ready"
    _put(stdscr, 0, 0, header, max(1, width - 1))
    _put(stdscr, 1, 0,
         "Agents: " + " / ".join(AGENTS) + " | highlighted: " + AGENTS[highlighted]
         + " | active: " + selected_agent,
         max(1, width - 1))

    transcript: list[str] = []
    if submitted is not None:
        transcript.extend([f"You  ·  {submitted.selected_agent}", submitted.task_text])
    if output:
        transcript.extend(["Output:", f"{submitted.selected_agent if submitted else selected_agent}  ·  response", output])
    chat_lines: list[str] = []
    for item in transcript:
        chat_lines.extend(_output_layout(item, chat_width))
    available = max(1, geo["chat_bottom"] - geo["chat_top"])
    chat_start, chat_stop, scroll = _output_view(len(chat_lines), output_scroll, available)
    for i, line in enumerate(chat_lines[chat_start:chat_stop]):
        _put(stdscr, geo["chat_top"] + i, 1, line, max(1, min(chat_width - 1, width - 2)))

    if geo["panel"]:
        x = width - geo["panel"]
        for row in range(2, geo["chat_bottom"]):
            if x < width - 1:
                _put(stdscr, row, x, "│", 1)
        if geo["chat_bottom"] > 2:
            _put(stdscr, 2, x + 2, "ACTIVITY", max(1, geo["panel"] - 3))
        entries = activity if activity else ["No activity yet"]
        capacity = max(0, geo["chat_bottom"] - 4)
        for row, entry in enumerate(entries[-capacity:] if capacity else [], start=4):
            _put(stdscr, row, x + 2, entry, max(1, geo["panel"] - 3))

    # Composer is always the last block before the shortcut footer.
    top = geo["composer_top"]
    _put(stdscr, top, 0, "─" * max(1, width - 1), max(1, width - 1))
    label = " Compose task (Ctrl-O newline) "
    _put(stdscr, top, 1, label, min(max(1, width - 2), len(label)))
    task_capacity = max(1, geo["composer_bottom"] - top - 2)
    start, stop = _task_view(task_layout, editor.cursor if editor else 0, task_capacity)
    visible = task_layout[start:stop]
    for i, (_, _, chunk) in enumerate(visible):
        lead = prefix if start + i == 0 else " " * len(prefix)
        _put(stdscr, top + 1 + i, 1, lead + chunk, max(1, width - 2))
    if len(visible) < len(task_layout) and top + 1 + len(visible) < geo["footer"]:
        _put(stdscr, top + 1 + len(visible), 1,
             f"lines {start + 1}-{stop}/{len(task_layout)}", max(1, width - 2))
    if editing_task:
        footer = "Tab to agent, q quit  •  Enter submit  •  Ctrl-O newline  •  PgUp/PgDn scroll"
    else:
        footer = "q quit  •  Y copy  •  S save  •  E Hermes engineering  •  Tab agent/task  •  Enter select  •  PgUp/PgDn scroll"
    _put(stdscr, geo["footer"], 0, footer, max(1, width - 1))
    if editing_task and editor is not None and visible and width > 2:
        for i, (line_start, line_end, _) in enumerate(visible):
            if line_start <= editor.cursor <= line_end:
                stdscr.move(min(top + 1 + i, geo["footer"] - 1),
                            min(1 + len(prefix) + editor.cursor - line_start, width - 2))
                break
    stdscr.refresh()
    return scroll


def _screen(stdscr: curses.window, workspace: Path) -> SubmittedTask | None:
    stdscr.keypad(True)
    conversations = ConversationStateStore()
    highlighted, selected_agent = 0, AGENTS[0]
    editor = _TaskEditor()
    editing_task = False
    submitted = None
    status, output, response = "", "", ""
    activity: list[str] = []
    message_count = 0
    output_scroll = 0
    worker: ExecutionWorker | None = None
    _render(stdscr, workspace, highlighted, selected_agent, editor.text, editing_task, submitted, editor=editor,
            output_scroll=output_scroll, activity=activity)
    while True:
        if worker is not None:
            request = worker.approvals.next_request()
            if request is not None:
                # Prompting and terminal mode changes stay on the curses thread.
                curses.endwin()
                try:
                    answer = input(request[1] + " ")
                except (EOFError, KeyboardInterrupt):
                    answer = None
                finally:
                    curses.reset_prog_mode()
                worker.approvals.answer(request, answer)
                output_scroll = _render(stdscr, workspace, highlighted, selected_agent,
                                        editor.text, editing_task, submitted, status, output,
                                        editor, output_scroll, activity)
            try:
                event = worker.events.get_nowait()
            except queue.Empty:
                pass
            else:
                if event.kind == "complete":
                    code, result, error = event.value[:3]
                    status = "Completed" if code == 0 else f"Error: {error.strip() or result.strip() or f'agent exited with status {code}'}"
                    output = result if code == 0 else ""
                    response = event.value[3] if code == 0 and len(event.value) > 3 else ""
                    activity.append(f"{worker.submitted.selected_agent} {'Completed' if code == 0 else 'Failed'}")
                    state = worker.submitted.conversation_state
                    if code == 0 and state is not None:
                        replies = [message for message in state.messages[message_count:]
                                   if message.role == "assistant"]
                        if replies:
                            activity.extend((f"{len(replies)} model turns",
                                             f"{sum(len(reply.tool_calls) for reply in replies)} tool calls"))
                else:
                    status = f"Error: execution failed ({type(event.value).__name__})"
                    activity.append(f"{worker.submitted.selected_agent} Failed")
                output_scroll = 0
                worker = None
                stdscr.timeout(-1)
                output_scroll = _render(stdscr, workspace, highlighted, selected_agent,
                                        editor.text, editing_task, submitted, status, output,
                                        editor, output_scroll, activity)
        key = stdscr.getch()
        if key == -1:
            continue
        if key == 9:
            editing_task = not editing_task
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE) and output:
            page = max(1, _layout(*stdscr.getmaxyx())["chat_bottom"] - 2)
            output_scroll += -page if key == curses.KEY_PPAGE else page
        elif editing_task:
            if key in (10, 13, curses.KEY_ENTER):
                if editor.text.strip() and worker is None:
                    submitted = SubmittedTask(
                        selected_agent, editor.text, workspace,
                        conversations.get(workspace, selected_agent)
                        if selected_agent.lower() in ("byte", "neighbor") else None,
                    )
                    message_count = (len(submitted.conversation_state.messages)
                                     if submitted.conversation_state is not None else 0)
                    editor = _TaskEditor(); status = f"Running {selected_agent}..."; output = ""; response = ""; output_scroll = 0
                    _render(stdscr, workspace, highlighted, selected_agent, editor.text, editing_task, submitted,
                            status, output, editor, output_scroll, activity)
                    try:
                        worker = ExecutionWorker(submitted)
                        worker.start()
                        activity.append(f"{selected_agent} started")
                        # Only poll input while a worker can publish events or
                        # approvals; idle input remains fully blocking.
                        stdscr.timeout(100)
                    except Exception as exc:  # noqa: BLE001
                        worker = None
                        status = f"Error: execution failed ({type(exc).__name__})"
                        activity.append(f"{selected_agent} Failed")
                else:
                    pass
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE) and output:
                page = max(1, stdscr.getmaxyx()[0] - 15)
                output_lines = _output_layout(output, stdscr.getmaxyx()[1])
                if key == curses.KEY_PPAGE:
                    output_scroll -= page
                else:
                    output_scroll += page
                _, _, output_scroll = _output_view(len(output_lines), output_scroll, page)
            elif key == 14:  # Ctrl-O is reliably distinct from Enter in curses terminals.
                editor.insert("\n")
            elif key in (curses.KEY_BACKSPACE, 8, 127):
                editor.backspace()
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN,
                         curses.KEY_HOME, curses.KEY_END):
                editor.move(key, width=max(1, stdscr.getmaxyx()[1]))
            elif 32 <= key <= 126:
                editor.insert(chr(key))
            else:
                continue
        elif key == ord("q"):
            if worker is None:
                return submitted
        elif key in (curses.KEY_DOWN, ord("j")):
            highlighted = (highlighted + 1) % len(AGENTS)
        elif key in (curses.KEY_UP, ord("k")):
            highlighted = (highlighted - 1) % len(AGENTS)
        elif key in (ord("Y"), ord("S")):
            if worker is not None:
                status = "Response unavailable while agent is running"
            elif not response:
                status = "No agent response to copy or save"
            elif key == ord("Y"):
                status = _copy_response(response)
            else:
                try:
                    path = _export_response(workspace, response)
                except (OSError, UnicodeError):
                    status = "Error: response could not be saved"
                else:
                    status = f"Saved response: {path.name}"
        elif key == ord("E") and selected_agent == "Hermes" and worker is None:
            if _confirm_engineering(stdscr, workspace):
                try:
                    code = _open_hermes_engineering(workspace)
                    status = "Hermes session closed" if code == 0 else f"Error: Hermes exited with status {code}"
                except (OSError, KeyboardInterrupt) as exc:
                    status = f"Error: Hermes could not start ({type(exc).__name__})"
            else:
                status = "Hermes engineering session cancelled"
        elif key in (10, 13, curses.KEY_ENTER):
            selected_agent = AGENTS[highlighted]
        else:
            continue
        output_scroll = _render(stdscr, workspace, highlighted, selected_agent, editor.text, editing_task,
                                submitted, status, output, editor, output_scroll, activity)


def run(workspace: Path) -> None:
    """Launch the screen on the real terminal and restore it on exit."""
    curses.wrapper(_screen, workspace)
