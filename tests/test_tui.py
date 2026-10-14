"""The first TUI slice: CLI registration and terminal-independent screen behavior."""

import curses
import importlib
import json
import threading
import time
from pathlib import Path
from typing import cast

import pytest

from forge.cli.main import build_parser, main
from forge.cli.render import EXIT_OK, EXIT_USAGE


def test_tui_command_accepts_explicit_workspace(tmp_path: Path) -> None:
    args = build_parser().parse_args(["tui", "--workspace", str(tmp_path)])
    assert args.subcommand == "tui"
    assert args.workspace == str(tmp_path)


def test_tui_requires_workspace_before_launch(monkeypatch) -> None:
    from forge.cli import tui

    def unexpected_launch(_path):
        raise AssertionError("screen should not open without a workspace")

    monkeypatch.setattr(tui, "run", unexpected_launch)
    import io

    err = io.StringIO()
    assert main(["tui"], environ={}, err=err) == EXIT_USAGE
    assert "no workspace configured" in err.getvalue()


def test_tui_command_launches_for_resolved_workspace(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    opened = []
    monkeypatch.setattr(tui, "run", opened.append)
    assert main(["tui", "--workspace", str(tmp_path)], environ={}) == EXIT_OK
    assert opened == [tmp_path.resolve()]


class FakeScreen:
    def __init__(self, keys: list[int | tuple[str, str | threading.Event, int]],
                 height: int = 24, width: int = 80) -> None:
        self.keys = iter(keys)
        self.height = height
        self.width = width
        self.writes: list[tuple[int, int, str]] = []
        self.refreshes = 0
        self.frames: list[str] = []
        self.keypad_enabled = False
        self.cursor: tuple[int, int] | None = None
        self.cursors: list[tuple[int, int] | None] = []
        self.timeout_ms = -1
        self.pending = None
        self.deadline = time.monotonic() + 5

    def keypad(self, enabled: bool) -> None:
        self.keypad_enabled = enabled

    def timeout(self, delay: int) -> None:
        self.timeout_ms = delay

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.writes.clear()

    def addnstr(self, y: int, x: int, text: str, n: int) -> None:
        assert 0 <= y < self.height
        assert n <= self.width - x - 1  # never write the bottom-right cell
        self.writes.append((y, x, text[:n]))

    def refresh(self) -> None:
        self.refreshes += 1
        self.frames.append("\n".join(text for _, _, text in self.writes))
        self.cursors.append(self.cursor)

    def move(self, y: int, x: int) -> None:
        self.cursor = (y, x)

    def getch(self) -> int:
        if self.pending is None:
            self.pending = next(self.keys)
        key = self.pending
        if isinstance(key, tuple) and key[0] == "wait_status":
            assert isinstance(key[1], str)
            if not any(key[1] in frame for frame in self.frames):
                assert self.timeout_ms > 0
                assert time.monotonic() < self.deadline, f"status {key[1]!r} was not redrawn"
                threading.Event().wait(self.timeout_ms / 1000)
                return -1
            key = key[2]
        self.pending = None
        if isinstance(key, tuple) and key[0] == "wait":
            assert isinstance(key[1], threading.Event)
            assert key[1].wait(1)
            return key[2]
        return cast(int, key)


@pytest.mark.parametrize(
    ("result", "expected"),
    [((0, "finished work", ""), "Completed"),
     ((4, "", "provider unavailable"), "Error: provider unavailable"),
     (RuntimeError("runner failed"), "Error: execution failed (RuntimeError)")],
)
def test_worker_result_redraws_without_another_keypress(
    tmp_path: Path, monkeypatch, result, expected: str
) -> None:
    from forge.cli import tui

    release = threading.Event()

    def execute(_submitted):
        assert release.wait(1)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tui, "_execute", execute)

    class WaitingScreen(FakeScreen):
        def __init__(self):
            super().__init__([9, ord("x"), 10])
            self.timeout_ms = None
            self.deadline = time.monotonic() + 2

        def timeout(self, delay: int) -> None:
            self.timeout_ms = delay

        def getch(self) -> int:
            try:
                return cast(int, next(self.keys))
            except StopIteration:
                if any(expected in frame for frame in self.frames):
                    return 9 if "Task [typing]" in self.frames[-1] else ord("q")
                assert self.timeout_ms is not None and 0 < self.timeout_ms <= 250
                release.set()
                assert time.monotonic() < self.deadline, "worker result was not redrawn"
                threading.Event().wait(self.timeout_ms / 1000)
                return -1

    screen = WaitingScreen()
    tui._screen(screen, tmp_path)
    assert any(expected in frame for frame in screen.frames)
    if expected == "Completed":
        assert any("finished work" in frame for frame in screen.frames)


@pytest.mark.parametrize("result, outcome", [
    ((0, "finished work", ""), "Completed"),
    ((4, "", "provider unavailable"), "Failed"),
    (RuntimeError("secret diagnostic"), "Failed"),
])
def test_activity_tracks_actual_worker_lifecycle_without_stale_running(
    tmp_path: Path, monkeypatch, result, outcome: str
) -> None:
    from forge.cli import tui

    release = threading.Event()

    def execute(_submitted):
        assert release.wait(1)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tui, "_execute", execute)

    class LifecycleScreen(FakeScreen):
        def __init__(self):
            super().__init__([9, ord("x"), 10], width=110)

        def getch(self):
            try:
                return super().getch()
            except StopIteration:
                if any(f"Neighbor {outcome}" in frame for frame in self.frames):
                    return 9 if "Task [typing]" in self.frames[-1] else ord("q")
                assert "Neighbor started" in self.frames[-1]
                assert "Running Neighbor..." in self.frames[-1]
                release.set()
                assert time.monotonic() < self.deadline
                threading.Event().wait(.01)
                return -1

    screen = LifecycleScreen()
    tui._screen(screen, tmp_path)  # type: ignore[arg-type]
    assert "Neighbor started" in screen.frames[-1]
    assert f"Neighbor {outcome}" in screen.frames[-1]
    assert "Running Neighbor..." not in screen.frames[-1]
    assert "model turns" not in screen.frames[-1]  # fake runner provided no counts


def test_activity_shows_observed_turns_and_tool_calls(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui
    from forge.models.scripted import ScriptedGateway, ScriptedTurn

    cli = importlib.import_module("forge.cli.main")
    monkeypatch.setattr(cli, "_build_model_gateway", lambda *a, **kw: ScriptedGateway([
        ScriptedTurn(tool_calls=(("not_a_tool", {}),)), ScriptedTurn(text="finished"),
    ]))
    screen = FakeScreen([9, ord("x"), 10,
                         ("wait_status", "Neighbor Completed", 9), ord("q")], width=110)
    tui._screen(screen, tmp_path)  # type: ignore[arg-type]
    assert "Neighbor started" in screen.frames[-1]
    assert "Neighbor Completed" in screen.frames[-1]
    assert "2 model turns" in screen.frames[-1]
    assert "1 tool call" in screen.frames[-1]
    assert "Running Neighbor..." not in screen.frames[-1]


def test_worker_start_failure_has_no_false_start_or_stale_status(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    def fail_start(self):
        raise RuntimeError("could not start")

    monkeypatch.setattr(tui.ExecutionWorker, "start", fail_start)
    screen = FakeScreen([9, ord("x"), 10, 9, ord("q")], width=110)
    tui._screen(screen, tmp_path)  # type: ignore[arg-type]
    assert "Neighbor Failed" in screen.frames[-1]
    assert "Neighbor started" not in screen.frames[-1]
    assert "Error: execution failed (RuntimeError)" in screen.frames[-1]
    assert "Running Neighbor..." not in screen.frames[-1]


def test_activity_reflows_and_clips_safely_on_resize(tmp_path: Path) -> None:
    from forge.cli.tui import _layout, _render

    screen = FakeScreen([], height=24, width=110)
    activity = ["Neighbor started", "Neighbor Completed", "2 model turns", "1 tool call"]
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, activity=activity)
    assert all(item in screen.frames[-1] for item in activity)
    screen.height, screen.width = 8, 75
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, activity=activity)
    assert "ACTIVITY" not in screen.frames[-1]
    assert all(y < screen.height and len(text) <= screen.width - x - 1
               for y, x, text in screen.writes)
    screen.height, screen.width = 12, 110
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, activity=activity)
    assert "ACTIVITY" in screen.frames[-1]
    composer_top = _layout(screen.height, screen.width)["composer_top"]
    assert all(y < composer_top for y, x, text in screen.writes
               if x >= screen.width - 24)
    assert all(y < screen.height and len(text) <= screen.width - x - 1
               for y, x, text in screen.writes)


def test_navigation_remains_responsive_while_worker_is_running(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    release = threading.Event()

    def execute(_submitted):
        assert release.wait(1)
        return 0, "done", ""

    monkeypatch.setattr(tui, "_execute", execute)

    class WaitingScreen(FakeScreen):
        def __init__(self):
            super().__init__([9, ord("x"), 10, 9, ord("q"), ord("j")])
            self.timeout_ms = None
            self.deadline = time.monotonic() + 2

        def timeout(self, delay: int) -> None:
            self.timeout_ms = delay

        def getch(self) -> int:
            try:
                return cast(int, next(self.keys))
            except StopIteration:
                if "Completed" in self.frames[-1]:
                    return ord("q")
                assert "Running Neighbor..." in self.frames[-1]
                assert "highlighted: Hermes" in self.frames[-1]
                release.set()
                assert time.monotonic() < self.deadline, "worker result was not redrawn"
                threading.Event().wait(self.timeout_ms / 1000)
                return -1

    screen = WaitingScreen()
    tui._screen(screen, tmp_path)
    assert any("Completed" in frame for frame in screen.frames)


def test_worker_approval_handoff_stays_on_main_thread(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    main_thread = threading.current_thread()
    modes = []
    monkeypatch.setattr(tui.curses, "endwin", lambda: modes.append(("suspend", threading.current_thread())))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: modes.append(("restore", threading.current_thread())))

    def approve(prompt):
        assert prompt == "approve? "
        assert threading.current_thread() is main_thread
        assert modes == [("suspend", main_thread)]
        return "y"

    monkeypatch.setattr("builtins.input", approve)

    def execute(_submitted, *, channel):
        assert threading.current_thread() is not main_thread
        assert channel.present(None, "approve?") == "y"
        return 0, "approved", ""

    monkeypatch.setattr(tui, "_execute", execute)
    screen = FakeScreen([9, ord("x"), 10,
                         ("wait_status", "Completed", 9), ord("q")])
    tui._screen(screen, tmp_path)

    assert modes == [("suspend", main_thread), ("restore", main_thread)]
    assert "approved" in screen.frames[-1]


def test_tui_shows_placeholder_workspace_and_quit_hint_then_exits_on_q(tmp_path: Path) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([ord("x"), ord("q")], width=len(str(tmp_path)) + 20)
    _screen(screen, tmp_path)
    rendered = "\n".join(text for _, _, text in screen.writes)
    assert "Forge | Agent: Neighbor" in rendered
    assert "Workspace: " in rendered
    assert "Tab agent/task" in rendered
    assert screen.refreshes == 1


def test_tui_abbreviates_workspace_in_small_terminal(tmp_path: Path) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([ord("q")], height=3, width=12)
    _screen(screen, tmp_path)
    assert screen.refreshes == 1
    assert screen.frames[0].startswith("Forge | Age")
    assert len(screen.writes[0][2]) <= screen.width - 1


def test_tui_starts_with_three_agents_and_one_selected(tmp_path: Path) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([ord("q")])
    _screen(screen, tmp_path)

    assert screen.keypad_enabled
    assert "Agent: Neighbor" in screen.frames[0]
    assert "Agents: Neighbor / Hermes / Byte" in screen.frames[0]
    assert "highlighted: Neighbor" in screen.frames[0]
    assert "Agent: Neighbor" in screen.frames[0]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (curses.KEY_DOWN, "Hermes"),
        (ord("j"), "Hermes"),
        (curses.KEY_UP, "Byte"),
        (ord("k"), "Byte"),
    ],
)
def test_tui_navigation_moves_highlight_without_confirming(
    tmp_path: Path, key: int, expected: str
) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([key, ord("q")])
    _screen(screen, tmp_path)

    assert len(screen.frames) == 2
    assert f"highlighted: {expected}" in screen.frames[1]
    assert "Agent: Neighbor" in screen.frames[1]
    assert "Agent: Neighbor" in screen.frames[1]


@pytest.mark.parametrize("enter", [10, 13, curses.KEY_ENTER])
def test_enter_confirms_highlighted_agent_without_leaving_menu(
    tmp_path: Path, enter: int
) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([ord("j"), enter, ord("j"), ord("q")], width=len(str(tmp_path)) + 20)
    _screen(screen, tmp_path)

    assert "Agent: Hermes" in screen.frames[2]
    assert "highlighted: Hermes" in screen.frames[2]
    assert "highlighted: Byte" in screen.frames[3]
    assert "Agent: Hermes" in screen.frames[3]
    assert "You" not in screen.frames[3]
    assert "Forge" in screen.frames[3]
    assert "Workspace: " in screen.frames[3]
    assert "Agent: Hermes" in screen.frames[3]
    assert "Tab agent/task" in screen.frames[3]


def test_tui_reserves_bottom_row_for_quit_hint(tmp_path: Path) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([ord("j"), ord("q")], height=10)
    _screen(screen, tmp_path)

    footer = [text for y, _, text in screen.writes if y == 9]
    assert len(footer) == 1
    assert "q" in footer[0]


def test_task_entry_submits_selected_agent_text_and_workspace(tmp_path: Path, monkeypatch) -> None:
    from forge.cli.tui import SubmittedTask, _screen
    from forge.cli import tui

    monkeypatch.setattr(tui, "_execute", lambda _submitted: (0, "done", ""))

    screen = FakeScreen([
        ord("j"), 10, 9, *map(ord, "fix q now"), 10,
        ("wait_status", "Completed", 9), ord("q")
    ])
    result = _screen(screen, tmp_path)

    assert result == SubmittedTask("Hermes", "fix q now", tmp_path)
    assert "You  ·  Hermes" in screen.frames[-1]
    assert "fix q now" in screen.frames[-1]
    assert "Hermes  ·  response" in screen.frames[-1]
    assert "done" in screen.frames[-1]
    assert "Completed" in screen.frames[-1]


@pytest.mark.parametrize("backspace", [curses.KEY_BACKSPACE, 8, 127])
def test_task_backspace_edits_without_changing_selection(tmp_path: Path, backspace: int, monkeypatch) -> None:
    from forge.cli.tui import SubmittedTask, _screen
    from forge.cli import tui

    monkeypatch.setattr(tui, "_execute", lambda _submitted: (0, "done", ""))

    screen = FakeScreen([9, *map(ord, "jobx"), backspace, ord(" "),
                         *map(ord, "done"), 10, ("wait_status", "Completed", 9), ord("q")])
    assert _screen(screen, tmp_path) == SubmittedTask("Neighbor", "job done", tmp_path)
    assert "You  ·  Neighbor" in screen.frames[-1]
    assert "job done" in screen.frames[-1]
    assert "jobx" not in screen.frames[-1]
    assert "Neighbor  ·  response" in screen.frames[-1]


def test_empty_task_is_not_submitted(tmp_path: Path) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([9, 10, ord(" "), 10, 9, ord("q")])
    assert _screen(screen, tmp_path) is None
    assert all("You  ·" not in frame for frame in screen.frames)
    assert all("Running" not in frame for frame in screen.frames)


def test_task_focus_explains_how_to_quit(tmp_path: Path) -> None:
    from forge.cli.tui import _screen

    screen = FakeScreen([9, ord("q"), 9, ord("q")])
    _screen(screen, tmp_path)
    assert "Tab to agent, q quit" in screen.frames[1]
    assert "Task [typing]: q" in screen.frames[2]
    assert "q quit" in screen.frames[3]


def test_task_cursor_follows_editing_position(tmp_path: Path) -> None:
    from forge.cli.tui import _layout, _screen, _task_layout

    screen = FakeScreen([9, ord("a"), ord("q"), 9, ord("q")])
    _screen(screen, tmp_path)
    composer_top = _layout(screen.height, screen.width,
                           len(_task_layout("aq", screen.width - 1)))["composer_top"]
    assert "Task [typing]: aq" in screen.frames[3]
    assert screen.cursors[3] == (composer_top + 1, 1 + len("Task [typing]: aq"))


def test_task_editor_wraps_and_keeps_text_within_width() -> None:
    from forge.cli.tui import _TaskEditor, _task_layout

    editor = _TaskEditor("Read forge/agents/neighbor.py and summarize Neighbor's responsibilities")
    lines = _task_layout(editor.text, 30)
    assert len(lines) > 1
    assert all(len(line[2]) <= 30 - 1 for line in lines)
    assert "".join(line[2] for line in lines) == editor.text


def test_task_editor_supports_cursor_editing_and_newlines() -> None:
    from forge.cli.tui import _TaskEditor

    editor = _TaskEditor("abcd")
    editor.move(curses.KEY_LEFT, 20)
    editor.backspace()
    editor.insert("X")
    editor.insert("\n")
    editor.insert("ef")
    assert editor.text == "abX\nefd"
    assert editor.cursor == len(editor.text) - 1


def test_task_editor_layout_changes_after_resize() -> None:
    from forge.cli.tui import _TaskEditor, _task_layout

    editor = _TaskEditor("a long task that needs to wrap")
    wide = _task_layout(editor.text, 40)
    narrow = _task_layout(editor.text, 16)
    assert len(wide) < len(narrow)
    editor.move(curses.KEY_UP, 16)
    assert 0 <= editor.cursor <= len(editor.text)


def test_tui_renders_all_wrapped_task_lines_when_they_fit(tmp_path: Path) -> None:
    from forge.cli.tui import _TaskEditor, _layout, _render, _task_layout

    text = "one two three four five six seven eight nine"
    screen = FakeScreen([], height=24, width=30)
    editor = _TaskEditor(text)
    _render(screen, tmp_path, 0, "Neighbor", text, True, None, editor=editor)

    layout = _task_layout(text, screen.width - 1)
    top = _layout(screen.height, screen.width, len(layout))["composer_top"]
    task_rows = [value for y, _, value in screen.writes if top < y < screen.height - 1]
    assert len(layout) >= 3
    assert len(task_rows) == len(layout)
    assert [row[-len(chunk):] for row, (_, _, chunk) in zip(task_rows, layout)] == [
        chunk for _, _, chunk in layout
    ]
    assert "".join(chunk for _, _, chunk in layout) == text
    assert all("lines " not in row for row in task_rows)


def test_tui_scrolls_long_task_to_keep_cursor_visible(tmp_path: Path) -> None:
    from forge.cli.tui import _TaskEditor, _layout, _render, _task_layout, _task_view

    text = "0123456789 " * 60
    screen = FakeScreen([], height=15, width=40)
    editor = _TaskEditor(text)
    _render(screen, tmp_path, 0, "Neighbor", text, True, None, editor=editor)

    layout = _task_layout(text, screen.width - 1)
    geo = _layout(screen.height, screen.width, len(layout))
    capacity = max(1, geo["composer_bottom"] - geo["composer_top"] - 2)
    start, stop = _task_view(layout, editor.cursor, capacity)
    assert len(layout) > capacity
    assert start > 0 and stop == len(layout)
    assert any(f"lines {start + 1}-{stop}/{len(layout)}" in value
               for _, _, value in screen.writes)
    assert screen.cursor == (geo["composer_top"] + 1 + stop - start - 1,
                             1 + len("Task [typing]: ") + len(layout[-1][2]))


def test_tui_wraps_long_output_and_preserves_multiline_shape(tmp_path: Path) -> None:
    from forge.cli.tui import _output_layout, _render

    output = "    indented " + "word " * 12 + "\n\nsecond paragraph"
    screen = FakeScreen([], height=24, width=30)
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, "Completed", output)
    rendered = "\n".join(text for _, _, text in screen.writes)
    layout = _output_layout(output, 30)
    assert len(layout) > 3
    assert "    indented" in rendered
    assert "second paragraph" in rendered
    assert "Output:" in rendered
    assert all(len(line) <= 29 for line in layout)


def test_tui_pages_through_long_output(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    output = "\n".join(f"response line {i}" for i in range(40))
    monkeypatch.setattr(tui, "_execute", lambda _submitted: (0, output, ""))
    screen = FakeScreen([9, *map(ord, "task"), 10,
                         ("wait_status", "Completed", curses.KEY_NPAGE),
                         curses.KEY_PPAGE, 9, ord("q")], height=16, width=40)
    tui._screen(screen, tmp_path)
    assert "You  ·  Neighbor" in screen.frames[-4]
    assert "response line 0" in screen.frames[-4]
    assert "response line 5" in screen.frames[-3]
    assert "response line 0" not in screen.frames[-3]
    assert "You  ·  Neighbor" in screen.frames[-2]
    assert "response line 0" in screen.frames[-2]


def test_tui_output_reflows_after_terminal_resize(tmp_path: Path) -> None:
    from forge.cli.tui import _output_layout, _render

    output = "a response with enough words to reflow when the terminal becomes narrow"
    screen = FakeScreen([], height=24, width=80)
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, "Completed", output)
    wide = len(_output_layout(output, screen.width))
    screen.width = 20
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, "Completed", output)
    assert len(_output_layout(output, screen.width)) > wide
    assert all(len(text) <= 19 for _, _, text in screen.writes)


@pytest.mark.parametrize(
    ("status", "width", "visible"),
    [
        ("Running Hermes...", 80, "Running Hermes..."),
        ("Completed", 80, "Completed"),
        ("Error: execution failed (RuntimeError)", 80,
         "Error: execution failed (RuntimeError)"),
        ("Error: forge: model provider returned an error (code=503)", 80,
         "Error: forge: model provider returned an error (code=503)"),
        ("Error: execution failed (RuntimeError)", 40, "Error: execution failed"),
        ("Running Hermes...", 12, "Running"),
        ("Completed", 12, "Completed"),
        ("Error: execution failed (RuntimeError)", 12, "Error:"),
    ],
)
def test_tui_prioritizes_execution_status_in_narrow_header(
    tmp_path: Path, status: str, width: int, visible: str
) -> None:
    from forge.cli.tui import _render

    screen = FakeScreen([], width=width)
    _render(screen, tmp_path, 0, "Neighbor", "", False, None, status)  # type: ignore[arg-type]
    assert visible in screen.frames[-1].splitlines()[0]


def test_submission_runs_selected_agent_and_shows_output(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    screen = FakeScreen([ord("j"), 10, 9, *map(ord, "fix q now"), 10, 9, ord("q")])
    calls = []
    execution_done = threading.Event()

    def execute(submitted):
        calls.append(submitted)
        assert "Running Hermes..." in screen.frames[-1]
        execution_done.set()
        return 0, "agent answer", ""

    monkeypatch.setattr(tui, "_execute", execute)
    screen.keys = iter([ord("j"), 10, 9, *map(ord, "fix q now"), 10,
                        ("wait_status", "Completed", 9), ord("q")])
    submitted = tui._screen(screen, tmp_path)

    assert calls == [tui.SubmittedTask("Hermes", "fix q now", tmp_path)]
    assert submitted == calls[0]
    assert "agent answer" in screen.frames[-1]

def test_engineering_requires_distinct_action_and_selected_hermes(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    opened = []
    monkeypatch.setattr(tui, "_open_hermes_engineering", opened.append, raising=False)
    screen = FakeScreen([ord("E"), ord("j"), ord("E"), ord("q")])
    tui._screen(screen, tmp_path)
    assert opened == []  # Highlight alone is not selection; no prompt or launch.
    assert all("Open Hermes Engineering Session?" not in frame for frame in screen.frames)

    screen = FakeScreen([ord("j"), 10, ord("E"), ord("n"), ord("q")])
    tui._screen(screen, tmp_path)
    assert opened == []
    assert any("Open Hermes Engineering Session?" in frame for frame in screen.frames)

def test_engineering_warning_and_cancellation_leave_forge_intact(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    monkeypatch.setattr(tui, "_open_hermes_engineering", lambda _: pytest.fail("cancelled session launched"))
    screen = FakeScreen([ord("j"), 10, ord("E"), ord("y"), ord("q")])
    assert tui._screen(screen, tmp_path) is None
    warning = next(frame for frame in screen.frames if "Open Hermes Engineering Session?" in frame)
    for phrase in ("native tools, credentials, and approval policy", "workspace does NOT confine",
                   "per-tool security", "Press Y to open Hermes"):
        assert phrase in warning
    assert "Hermes engineering session cancelled" in screen.frames[-1]
    assert "Agent: Hermes" in screen.frames[-1]

def test_engineering_confirmation_preserves_submitted_text_route_and_editor(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    calls = []
    monkeypatch.setattr(tui, "_execute", lambda submitted: (calls.append(submitted) or (0, "text answer", "")))
    monkeypatch.setattr(tui, "_open_hermes_engineering", lambda path: (calls.append(path) or 0))
    screen = FakeScreen([ord("j"), 10, 9, *map(ord, "hello"), 10,
                         ("wait_status", "Completed", 9), ord("E"), ord("Y"), ord("q")])
    submitted = tui._screen(screen, tmp_path)
    assert calls == [tui.SubmittedTask("Hermes", "hello", tmp_path), tmp_path]
    assert submitted == calls[0]
    assert "text answer" in screen.frames[-1]
    assert "You  ·  Hermes" in screen.frames[-1]
    assert "Hermes session closed" in screen.frames[-1]

def test_failed_text_only_request_never_escalates_or_opens_confirmation(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    calls = []
    monkeypatch.setattr(tui, "_execute", lambda submitted: (calls.append(submitted) or (2, "", "text-only refusal")))
    monkeypatch.setattr(tui, "_open_hermes_engineering", lambda _: pytest.fail("automatic escalation"))
    screen = FakeScreen([ord("j"), 10, 9, *map(ord, "run `pytest`"), 10,
                         ("wait_status", "Error: text-only refusal", 9), ord("q")])
    tui._screen(screen, tmp_path)
    assert calls == [tui.SubmittedTask("Hermes", "run `pytest`", tmp_path)]
    assert "Error: text-only refusal" in screen.frames[-1]
    assert all("Open Hermes Engineering Session?" not in frame for frame in screen.frames)

def test_engineering_return_keeps_unsubmitted_draft_and_selected_agent(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    monkeypatch.setattr(tui, "_open_hermes_engineering", lambda _: 0)
    screen = FakeScreen([ord("j"), 10, 9, *map(ord, "draft"), 9,
                         ord("E"), ord("Y"), ord("q")])
    assert tui._screen(screen, tmp_path) is None
    assert "Agent: Hermes" in screen.frames[-1]
    assert "Task [Tab to type]: draft" in screen.frames[-1]

def test_engineering_launch_uses_interactive_native_hermes_and_restores_terminal(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    main_thread = threading.current_thread()
    steps = []
    monkeypatch.setattr(tui.shutil, "which", lambda name: "/installed/bin/hermes" if name == "hermes" else None)
    monkeypatch.setattr(tui.curses, "endwin", lambda: steps.append(("suspend", threading.current_thread())))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: steps.append(("restore", threading.current_thread())))
    monkeypatch.setenv("FORGE_OPENROUTER_API_KEY", "not-for-child")
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")

    def fake_run(argv, **kwargs):
        assert steps == [("suspend", main_thread)]
        assert argv == ["/installed/bin/hermes", "chat", "--cli", "--in", str(tmp_path)]
        assert kwargs["cwd"] == tmp_path
        assert kwargs["check"] is False
        assert "stdin" not in kwargs and "stdout" not in kwargs and "stderr" not in kwargs
        assert "capture_output" not in kwargs and "text" not in kwargs
        assert "FORGE_OPENROUTER_API_KEY" not in kwargs["env"]
        assert "HERMES_YOLO_MODE" not in kwargs["env"]
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(tui.subprocess, "run", fake_run)
    screen = FakeScreen([ord("j"), 10, ord("E"), ord("Y"), ord("q")])
    tui._screen(screen, tmp_path)
    assert steps == [("suspend", main_thread), ("restore", main_thread)]
    assert "Hermes session closed" in screen.frames[-1]

@pytest.mark.parametrize("failure, expected", [
    (FileNotFoundError("missing Hermes"), "FileNotFoundError"),
    (OSError("could not execute"), "OSError"),
    (KeyboardInterrupt(), "KeyboardInterrupt"),
])
def test_engineering_launch_error_restores_forge(tmp_path: Path, monkeypatch, failure, expected) -> None:
    from forge.cli import tui

    steps = []
    monkeypatch.setattr(tui.shutil, "which", lambda _: "/installed/bin/hermes")
    monkeypatch.setattr(tui.curses, "endwin", lambda: steps.append("suspend"))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: steps.append("restore"))

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(tui.subprocess, "run", fail)
    screen = FakeScreen([ord("j"), 10, ord("E"), ord("Y"), ord("q")])
    tui._screen(screen, tmp_path)
    assert steps == ["suspend", "restore"]
    assert f"Error: Hermes could not start ({expected})" in screen.frames[-1]
    assert "Agent: Hermes" in screen.frames[-1]

def test_missing_hermes_never_suspends_or_spawns(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    monkeypatch.setattr(tui.shutil, "which", lambda _: None)
    monkeypatch.setattr(tui.curses, "endwin", lambda: pytest.fail("suspended without Hermes"))
    monkeypatch.setattr(tui.subprocess, "run", lambda *a, **kw: pytest.fail("spawned without Hermes"))
    screen = FakeScreen([ord("j"), 10, ord("E"), ord("Y"), ord("q")])
    tui._screen(screen, tmp_path)
    assert "FileNotFoundError" in screen.frames[-1]


@pytest.mark.parametrize("agent", ["Neighbor", "Hermes", "Byte"])
def test_tui_dispatches_through_existing_cli_with_task_and_workspace(
    tmp_path: Path, monkeypatch, capsys, agent: str
) -> None:
    from forge.cli import tui
    from forge.tools.approval import TerminalApprovalChannel

    cli = importlib.import_module("forge.cli.main")
    terminal_modes = []
    monkeypatch.setattr(tui.curses, "endwin", lambda: terminal_modes.append("suspend"))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: terminal_modes.append("restore"))
    calls = []

    def fake_main(argv, *, out, err, channel):
        calls.append((argv, channel))
        assert terminal_modes == []
        out.write("from existing runner")
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    submitted = tui.SubmittedTask(agent, "fix the tests", tmp_path)
    assert tui._execute(submitted) == (0, "from existing runner", "")
    assert calls[0][0] == [agent.lower(), "--workspace", str(tmp_path), "fix the tests"]
    assert isinstance(calls[0][1], TerminalApprovalChannel)
    assert terminal_modes == []
    assert f"Running {agent}..." in capsys.readouterr().out


def test_worker_execution_does_not_print_over_curses_after_return(tmp_path: Path, monkeypatch, capsys) -> None:
    from forge.cli import tui

    cli = importlib.import_module("forge.cli.main")
    monkeypatch.setattr(cli, "main", lambda argv, **kwargs: 0)
    assert tui._execute(tui.SubmittedTask("Neighbor", "task", tmp_path), channel=object()) == (0, "", "")
    assert capsys.readouterr().out == ""


def test_tui_suspends_only_for_approval_and_restores_after_approval(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    modes = []
    monkeypatch.setattr(tui.curses, "endwin", lambda: modes.append("suspend"))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: modes.append("restore"))
    cli = importlib.import_module("forge.cli.main")

    def fake_main(argv, *, out, err, channel):
        assert modes == []
        channel._read = lambda _prompt: "y"
        assert channel.present(None, "approval") == "y"
        assert modes == ["suspend", "restore"]
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    assert tui._execute(tui.SubmittedTask("Byte", "task", tmp_path))[0] == 0
    assert modes == ["suspend", "restore"]


def test_tui_rejection_restores_terminal_without_approving(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    modes = []
    monkeypatch.setattr(tui.curses, "endwin", lambda: modes.append("suspend"))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: modes.append("restore"))
    cli = importlib.import_module("forge.cli.main")

    def fake_main(argv, *, out, err, channel):
        channel._read = lambda _prompt: "n"
        assert channel.present(None, "approval") == "n"
        assert modes == ["suspend", "restore"]
        return 3

    monkeypatch.setattr(cli, "main", fake_main)
    assert tui._execute(tui.SubmittedTask("Byte", "task", tmp_path))[0] == 3
    assert modes == ["suspend", "restore"]


def test_tui_restores_terminal_when_approval_prompt_errors(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    modes = []
    monkeypatch.setattr(tui.curses, "endwin", lambda: modes.append("suspend"))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: modes.append("restore"))
    cli = importlib.import_module("forge.cli.main")

    def fake_main(argv, *, out, err, channel):
        channel._read = lambda _prompt: "n"
        channel.present(None, "approval")
        raise RuntimeError("runner failed")

    monkeypatch.setattr(cli, "main", fake_main)
    with pytest.raises(RuntimeError):
        tui._execute(tui.SubmittedTask("Byte", "task", tmp_path))
    assert modes == ["suspend", "restore"]


def test_tui_shows_runner_failure_without_crashing(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    monkeypatch.setattr(tui, "_execute", lambda _submitted: (4, "", "forge: provider unavailable"))
    screen = FakeScreen([9, ord("h"), ord("i"), 10,
                         ("wait_status", "Error: forge: provider unavailable", 9), ord("q")])
    tui._screen(screen, tmp_path)
    assert any("Running Neighbor..." in frame for frame in screen.frames)
    assert "Error: forge: provider unavailable" in screen.frames[-1]


def test_tui_restores_terminal_on_runner_exception(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    cli = importlib.import_module("forge.cli.main")

    modes = []
    monkeypatch.setattr(tui.curses, "endwin", lambda: modes.append("suspend"))
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: modes.append("restore"))

    def broken_main(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "main", broken_main)
    with pytest.raises(RuntimeError):
        tui._execute(tui.SubmittedTask("Byte", "hello", tmp_path))
    assert modes == []


def test_tui_reports_unexpected_execution_error_and_stays_open(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui

    def broken_execute(_submitted):
        raise RuntimeError("secret diagnostic")

    monkeypatch.setattr(tui, "_execute", broken_execute)
    execution_done = threading.Event()
    original_broken_execute = broken_execute

    def synchronized_execute(submitted):
        try:
            return original_broken_execute(submitted)
        finally:
            execution_done.set()

    monkeypatch.setattr(tui, "_execute", synchronized_execute)
    screen = FakeScreen([9, ord("h"), ord("i"), 10,
                         ("wait_status", "Error: execution failed (RuntimeError)", 9), ord("q")])
    tui._screen(screen, tmp_path)
    assert "Error: execution failed (RuntimeError)" in screen.frames[-1]
    assert "secret diagnostic" not in screen.frames[-1]


def test_neighbor_tui_run_reports_provider_503_and_can_submit_again(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercise screen -> CLI -> native runner -> routed OpenRouter gateway."""
    from forge.cli import tui
    from forge.models.openrouter import OpenRouterGateway

    config = tmp_path / "models.toml"
    config.write_text(
        '[providers.openrouter]\ncredential_env = "FORGE_OPENROUTER_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "vendor/neighbor", provider = "openrouter" }\n'
        '[routing]\ndefault_tier = "general_light"\n'
        '[routing.tasks.conversation]\nstandard = "general_light"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FORGE_MODELS_CONFIG", str(config))
    monkeypatch.setenv("FORGE_OPENROUTER_API_KEY", "test-only-key")
    monkeypatch.delenv("FORGE_MODEL", raising=False)
    monkeypatch.setattr(tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(tui.curses, "reset_prog_mode", lambda: None)

    requested_models = []

    def provider_reply(self, url, body, headers, timeout):
        requested_models.append(json.loads(body)["model"])
        if len(requested_models) == 1:
            return 200, '{"error": {"code": 503}}'
        return 200, '{"choices": [{"message": {"content": "Neighbor answered."}}]}'

    monkeypatch.setattr(OpenRouterGateway, "_post", provider_reply)
    second_response = threading.Event()

    def synchronized_provider_reply(self, url, body, headers, timeout):
        response = provider_reply(self, url, body, headers, timeout)
        if len(requested_models) == 2:
            second_response.set()
        return response

    monkeypatch.setattr(OpenRouterGateway, "_post", synchronized_provider_reply)
    screen = FakeScreen([9, *map(ord, "hi"), 10,
                         ("wait_status", "Error: forge: model provider returned an error (code=503)", ord("h")),
                         ord("i"), 10, ("wait_status", "Completed", 9), ord("q")])
    submitted = tui._screen(screen, tmp_path)  # type: ignore[arg-type]

    assert submitted == tui.SubmittedTask("Neighbor", "hi", tmp_path)
    assert requested_models == ["vendor/neighbor", "vendor/neighbor"]
    assert any("Error: forge: model provider returned an error (code=503)" in frame
               for frame in screen.frames)
    assert "Completed" in screen.frames[-1]
    assert "Neighbor answered." in screen.frames[-1]


def test_two_tui_messages_reach_model_with_prior_structured_history(tmp_path: Path, monkeypatch) -> None:
    from forge.cli import tui
    from forge.models.scripted import ScriptedGateway, ScriptedTurn
    from forge.models.base import ToolCall
    cli = importlib.import_module("forge.cli.main")
    gateways = []
    turns = [
        [ScriptedTurn(tool_calls=(("not_a_tool", {}),)), ScriptedTurn(text="first answer")],
        [ScriptedTurn(text="second answer")],
    ]

    def gateway(*args, **kwargs):
        model = ScriptedGateway(turns[len(gateways)])
        gateways.append(model)
        return model

    monkeypatch.setattr(cli, "_build_model_gateway", gateway)

    class TwoTurnScreen(FakeScreen):
        def __init__(self):
            super().__init__([9, *map(ord, "first"), 10])
            self.stage = 0

        def getch(self):
            try:
                return super().getch()
            except StopIteration:
                completed = sum("Completed" in frame for frame in self.frames)
                if self.stage == 0 and completed >= 1:
                    self.stage = 1
                    self.keys = iter([*map(ord, "second"), 10])
                    return super().getch()
                if self.stage == 1 and any("second answer" in frame for frame in self.frames):
                    self.stage = 2
                    self.keys = iter([9, ord("q")])
                    return super().getch()
                assert time.monotonic() < self.deadline
                threading.Event().wait(.01)
                return -1

    tui._screen(TwoTurnScreen(), tmp_path)  # type: ignore[arg-type]
    assert len(gateways) == 2
    messages = gateways[1].last_request.messages
    assert [(m.role, m.content) for m in messages] == [
        ("user", "first"), ("assistant", ""), ("tool", messages[2].content),
        ("assistant", "first answer"), ("user", "second"),
    ]
    assert messages[1].tool_calls == (ToolCall("call_1", "not_a_tool", "{}"),)
    assert messages[2].tool_call_id == "call_1"


def test_tui_history_is_scoped_by_normalized_workspace_and_agent(tmp_path: Path, monkeypatch) -> None:
    from forge.agents.state import ConversationStateStore
    from forge.cli import tui
    from forge.models.scripted import ScriptedGateway, ScriptedTurn
    cli = importlib.import_module("forge.cli.main")
    gateways = []

    def gateway(*args, **kwargs):
        model = ScriptedGateway([ScriptedTurn(text="ok")])
        gateways.append(model)
        return model

    monkeypatch.setattr(cli, "_build_model_gateway", gateway)
    other = tmp_path / "other"
    other.mkdir()
    store = ConversationStateStore()

    def submit(agent, workspace, text):
        state = store.get(workspace, agent)
        task = tui.SubmittedTask(agent, text, workspace, state)
        assert tui._execute(task, channel=object())[0] == EXIT_OK
        return gateways[-1].last_request.messages

    assert len(submit("Neighbor", tmp_path, "neighbor first")) == 1
    assert len(submit("Byte", tmp_path, "byte first")) == 1
    assert len(submit("Neighbor", other, "other first")) == 1
    assert [m.content for m in submit("Neighbor", tmp_path / ".", "neighbor next")] == [
        "neighbor first", "ok", "neighbor next",
    ]
    assert [m.content for m in submit("Byte", tmp_path, "byte next")] == [
        "byte first", "ok", "byte next",
    ]


def test_failed_tui_turn_does_not_poison_next_turn(tmp_path: Path, monkeypatch) -> None:
    from forge.agents.state import ConversationStateStore
    from forge.cli import tui
    from forge.models.base import GatewayError
    from forge.models.scripted import ScriptedGateway, ScriptedTurn
    cli = importlib.import_module("forge.cli.main")
    gateways = []
    scripts = [
        [ScriptedTurn(text="baseline")],
        [ScriptedTurn(tool_calls=(("not_a_tool", {}),)),
         ScriptedTurn(raises=GatewayError("offline"))],
        [ScriptedTurn(text="recovered")],
    ]

    def gateway(*args, **kwargs):
        model = ScriptedGateway(scripts[len(gateways)])
        gateways.append(model)
        return model

    monkeypatch.setattr(cli, "_build_model_gateway", gateway)
    state = ConversationStateStore().get(tmp_path, "Neighbor")

    def submit(text):
        return tui._execute(tui.SubmittedTask("Neighbor", text, tmp_path, state), channel=object())

    assert submit("first")[0] == EXIT_OK
    assert submit("failed")[0] != EXIT_OK
    assert submit("retry")[0] == EXIT_OK
    assert [(m.role, m.content) for m in gateways[-1].last_request.messages] == [
        ("user", "first"), ("assistant", "baseline"), ("user", "retry"),
    ]


def test_standalone_cli_tasks_remain_stateless(tmp_path: Path, monkeypatch) -> None:
    import io
    from forge.models.scripted import ScriptedGateway, ScriptedTurn
    cli = importlib.import_module("forge.cli.main")
    gateways = []

    def gateway(*args, **kwargs):
        model = ScriptedGateway([ScriptedTurn(text="ok")])
        gateways.append(model)
        return model

    monkeypatch.setattr(cli, "_build_model_gateway", gateway)
    for text in ("first", "second"):
        assert main(["neighbor", "--workspace", str(tmp_path), text], out=io.StringIO(),
                    err=io.StringIO(), environ={}) == EXIT_OK
    assert [[m.content for m in model.last_request.messages] for model in gateways] == [
        ["first"], ["second"],
    ]
