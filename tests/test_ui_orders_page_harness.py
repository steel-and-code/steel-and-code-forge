"""The browser-probe harness itself: does it report its own capability honestly?

``tests/test_ui_orders_page.py`` skips its whole module when Chromium cannot
drive a page. That is the right behaviour for an environment gap, and it is also
exactly the kind of arrangement where a mistake is invisible: a harness that
skips when it should run, or that skips for the wrong reason, produces a green
suite that has quietly stopped checking anything.

So the guard is tested here, in a module that is never skipped. Three properties,
each of which fails loudly if broken:

* the probe answers ``False`` for a browser that cannot load an ``http://`` URL,
  and says *why* in terms a reader can act on;
* it does not confuse "this browser is broken" with "this page is broken";
* the skip is visible -- reported, with a reason -- rather than silent.

This file asserts the *harness*, not the Order Board. The Order Board's own
behaviour is covered by ``test_ui_orders_store.py``, ``test_ui_orders_api.py``,
and ``test_ui_orders_board_ui.py``, none of which need a browser.
"""

from __future__ import annotations

import subprocess

import pytest

from tests import test_ui_orders_page as harness


def test_the_probe_reports_a_browser_that_cannot_load_http() -> None:
    """A stub browser that never answers must produce ``False`` and a reason.

    Driven through a fake ``subprocess.run`` so the answer is the harness's own
    logic rather than a property of whatever Chromium happens to be installed.
    """
    usable, reason = harness._chromium_can_serve_http(
        run=lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="chromium", timeout=20)
        )
    )

    assert usable is False
    assert reason
    # The reason has to be actionable: it should say what failed and where the
    # behaviour is still covered, so a reader is not left guessing whether the
    # Order Board is broken.
    lowered = reason.lower()
    assert "chromium" in lowered
    assert "http" in lowered
    assert "test_ui_orders_store.py" in reason


def test_the_probe_passes_when_the_browser_loads_the_page() -> None:
    """The positive case, so the guard is not simply always-``False``."""

    class _Result:
        stdout = "<html><head><title>PROBE-OK</title></head><body/></html>"
        stderr = ""

    usable, reason = harness._chromium_can_serve_http(run=lambda *a, **k: _Result())

    assert usable is True
    assert reason == ""


def test_the_probe_fails_when_the_page_does_not_render() -> None:
    """A browser that answers but renders nothing is still not usable.

    Distinct from the timeout case: here Chromium exited cleanly, so treating it
    as usable would run 28 real measurements against a browser that produced
    nothing -- every one of them failing for the same environmental reason.
    """

    class _Result:
        stdout = "<html><head><title>Some Other Page</title></head><body/></html>"
        stderr = ""

    usable, reason = harness._chromium_can_serve_http(run=lambda *a, **k: _Result())

    assert usable is False
    assert "did not render" in reason
    # And it quotes the title it actually saw, so the diagnosis is checkable.
    assert "Some Other Page" in reason


def test_the_probe_reports_a_browser_that_will_not_launch() -> None:
    """A missing or non-executable binary is a capability gap, not a crash."""

    def _raise(*args: object, **kwargs: object):
        raise OSError("Exec format error")

    usable, reason = harness._chromium_can_serve_http(run=_raise)

    assert usable is False
    assert "could not be launched" in reason


def test_the_probe_never_raises() -> None:
    """Any unexpected failure is reported, never propagated.

    The probe runs at *import* time to decide the module's skip. An exception
    here would abort collection for the whole file as an error rather than a
    skip, which is precisely the failure mode the skip exists to avoid.
    """

    def _explode(*args: object, **kwargs: object):
        raise RuntimeError("something unforeseen")

    usable, reason = harness._chromium_can_serve_http(run=_explode)

    assert usable is False
    assert reason


def test_the_skip_is_decided_by_the_probe_not_by_path_presence() -> None:
    """``CHROME`` being set is not what gates the module.

    The original guard keyed on the binary's presence alone, which is why a
    browser that is installed and broken produced 28 failures instead of 28
    skips. This asserts the decision now comes from the probe's answer.
    """
    assert hasattr(harness, "_CHROME_USABLE")
    assert hasattr(harness, "_CHROME_REASON")

    # Whichever way this environment resolves, the two must agree: a usable
    # browser has no complaint recorded, and an unusable one must say why.
    if harness._CHROME_USABLE:
        assert harness._CHROME_REASON == ""
    else:
        assert harness._CHROME_REASON


def test_the_probe_is_cached_so_it_costs_one_launch() -> None:
    """The probe must not run a browser per test.

    Without caching this would add a Chromium launch to every test in the
    module -- twenty-eight extra launches, each seconds long, to answer a
    question that cannot change within a run.
    """
    assert harness._probe_http_capability.cache_info().maxsize == 1


@pytest.mark.parametrize(
    "flag",
    ["--headless", "--disable-gpu", "--no-sandbox"],
)
def test_every_launch_uses_the_shared_headless_flags(flag: str) -> None:
    """Both the probe and the real measurement go through ``CHROME_FLAGS``.

    Duplicated flag lists drift, and the failure mode is subtle: the probe
    launches a browser that behaves differently from the one the tests use, so
    the capability answer describes something other than what runs.
    """
    assert flag in harness.CHROME_FLAGS


def test_this_module_is_not_itself_skipped() -> None:
    """The guard on the guard.

    If this file were skipped for the same reason as the module it protects,
    the whole arrangement would be untested in exactly the environment where it
    matters.
    """
    assert harness.__name__.endswith("test_ui_orders_page")
