import io
import json
import threading

import pytest

from conftest import REAL_FETCH_LATEST_RELEASE as real_fetch, wait_until


# ── version comparison ───────────────────────────────────────────────────────

@pytest.mark.parametrize("tag,current,newer", [
    ("v1.3.1", "1.3.0", True),
    ("v1.4.0", "1.3.1", True),
    ("v2.0", "1.9.9", True),
    ("v1.3.0", "1.3.0", False),            # same version
    ("v1.3", "1.3.0", False),              # "v1.3" means 1.3.0
    ("v1.2", "1.3.0", False),              # older
    ("v1.3.0", "1.4.0-dev", False),        # a test build is ahead of the last release
    ("v1.4.0", "1.4.0-dev", True),         # ...until that version is released
    ("v1.5-beta", "1.3.0", False),         # pre-releases are never offered
    ("nightly", "1.3.0", False),
    ("", "1.3.0", False),
    (None, "1.3.0", False),
])
def test_is_newer_release(appmod, tag, current, newer):
    assert appmod.is_newer_release(tag, current) is newer


def test_parse_version(appmod):
    assert appmod.parse_version("v1.3.0") == ((1, 3, 0), True)
    assert appmod.parse_version("1.4.0-dev") == ((1, 4, 0), False)
    assert appmod.parse_version("v1.3-beta") == ((1, 3, 0), False)
    assert appmod.parse_version("garbage") is None


def test_pretend_version_for_testing(appmod, monkeypatch):
    assert appmod.current_version() == appmod.APP_VERSION
    monkeypatch.setenv(appmod.PRETEND_VERSION_ENV, "1.2.0")
    assert appmod.current_version() == "1.2.0"


# ── reading GitHub's answer ──────────────────────────────────────────────────

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _serve(monkeypatch, payload, seen=None):
    import urllib.request

    def urlopen(req, timeout=None):
        if seen is not None:
            seen.append(req)
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_fetch_reads_github_latest_release(appmod, monkeypatch):
    seen = []
    _serve(monkeypatch, {
        "tag_name": "v1.4.1", "draft": False, "prerelease": False,
        "html_url": "https://github.com/ELSEGuy/silenceplayer/releases/tag/v1.4.1"}, seen)
    info = real_fetch()
    assert info == appmod.ReleaseInfo("1.4.1", "v1.4.1",
                                      "https://github.com/ELSEGuy/silenceplayer/releases/tag/v1.4.1")
    assert seen[0].full_url == appmod.UPDATE_API_URL
    assert seen[0].get_header("User-agent").startswith("SilencePlayer/")


def test_fetch_ignores_prereleases_and_bad_data(appmod, monkeypatch):
    _serve(monkeypatch, {"tag_name": "v1.5-beta", "prerelease": True})
    assert real_fetch() is None
    _serve(monkeypatch, {"tag_name": 5})
    assert real_fetch() is None
    _serve(monkeypatch, ["not", "a", "dict"])
    assert real_fetch() is None


def test_fetch_never_opens_a_foreign_link(appmod, monkeypatch):
    _serve(monkeypatch, {"tag_name": "v1.4.1", "html_url": "https://evil.example/download"})
    assert real_fetch().url == appmod.RELEASES_URL


# ── the checker ──────────────────────────────────────────────────────────────

class Results:
    def __init__(self):
        self.calls = []

    def __call__(self, info, error, manual):
        self.calls.append((info, error, manual))


def _release(appmod, tag="v1.4.1"):
    return appmod.ReleaseInfo(tag.lstrip("v"), tag, appmod.RELEASES_URL)


def test_newer_release_is_reported(appmod, tmp_path):
    res = Results()
    c = appmod.UpdateChecker("1.3.0", res, fetch=lambda: _release(appmod),
                             state_path=str(tmp_path / "u.json"))
    assert c.check() is True
    assert res.calls == [(_release(appmod), None, False)]


def test_up_to_date_reports_nothing_new(appmod, tmp_path):
    res = Results()
    c = appmod.UpdateChecker("1.4.1", res, fetch=lambda: _release(appmod),
                             state_path=str(tmp_path / "u.json"))
    c.check()
    assert res.calls == [(None, None, False)]


def test_network_error_is_reported_not_raised(appmod, tmp_path):
    res = Results()

    def broken():
        raise OSError("no internet")

    c = appmod.UpdateChecker("1.3.0", res, fetch=broken, state_path=str(tmp_path / "u.json"))
    assert c.check() is False
    info, err, manual = res.calls[0]
    assert info is None and isinstance(err, OSError)


def test_skipped_version_is_remembered_across_restarts(appmod, tmp_path):
    path = str(tmp_path / "sub" / "u.json")
    res = Results()
    c = appmod.UpdateChecker("1.3.0", res, fetch=lambda: _release(appmod), state_path=path)
    c.skip("v1.4.1")
    again = appmod.UpdateChecker("1.3.0", res, fetch=lambda: _release(appmod), state_path=path)
    again.check()
    assert res.calls[-1][0] is None                       # automatic check: stays quiet
    again.check(manual=True)
    assert res.calls[-1][0] == _release(appmod)           # "Check now" still shows it
    newer = appmod.UpdateChecker("1.3.0", res, fetch=lambda: _release(appmod, "v1.4.2"),
                                 state_path=path)
    newer.check()
    assert res.calls[-1][0].tag == "v1.4.2"               # a later version is offered again


def test_corrupt_state_file_is_ignored(appmod, tmp_path):
    path = tmp_path / "u.json"
    path.write_text("{not json")
    c = appmod.UpdateChecker("1.3.0", Results(), fetch=lambda: None, state_path=str(path))
    assert c.skipped is None


def test_background_thread_checks_and_respects_the_switch(appmod, tmp_path, monkeypatch):
    monkeypatch.setattr(appmod.UpdateChecker, "FIRST_DELAY", 0.01)
    calls = []
    enabled = {"on": False}

    def fetch():
        calls.append(1)
        return _release(appmod)

    res = Results()
    c = appmod.UpdateChecker("1.3.0", res, enabled=lambda: enabled["on"], fetch=fetch,
                             state_path=str(tmp_path / "u.json"))
    c.start()
    try:
        threading.Event().wait(0.2)
        assert calls == [], "checked although updates are switched off"
        c.check_now()                                       # "Check now" works even when off
        assert wait_until(lambda: len(res.calls) == 1)
        assert res.calls[0][2] is True
    finally:
        c.stop()
    c._thread.join(2)
    assert not c._thread.is_alive()


# ── in the app ───────────────────────────────────────────────────────────────

from test_ui import cfg_path, make_app, pump, root  # noqa: E402,F401  (fixtures too)


@pytest.fixture
def ui(appmod, root, cfg_path, fast_timing, monkeypatch):
    opened = []
    monkeypatch.setattr(appmod.webbrowser, "open", lambda url: opened.append(url))
    a = make_app(appmod, root)
    yield a, opened
    a.updater.stop()
    a.monitor.stop(wait=True)


def test_notice_appears_and_download_opens_the_release_page(appmod, root, ui):
    a, opened = ui
    h0 = a.root.winfo_height()
    a._on_update_result(_release(appmod), None, False)
    pump(root, 0.1)
    assert a._bar_shown(a.update_row)
    assert "v1.4.1 is available" in a.update_label.cget("text")
    assert a.root.winfo_height() > h0, "window grows so the bar doesn't cover settings"
    assert "v1.4.1 is available" in a.update_status.cget("text")
    a._open_update()
    assert opened == [appmod.RELEASES_URL]
    assert not a._bar_shown(a.update_row)


def test_later_hides_until_next_launch_but_check_now_shows_it(appmod, root, ui):
    a, opened = ui
    a._on_update_result(_release(appmod), None, False)
    a._update_later_clicked()
    a._on_update_result(_release(appmod), None, False)       # 6 hours later
    assert not a._bar_shown(a.update_row)
    assert a._update_info is not None                         # tray item still offers it
    a._on_update_result(_release(appmod), None, True)         # Advanced > Check now
    assert a._bar_shown(a.update_row)


def test_skip_is_saved_without_save_settings(appmod, root, ui, cfg_path, tmp_path):
    a, opened = ui
    a._on_update_result(_release(appmod), None, False)
    a._skip_update()
    assert not a._bar_shown(a.update_row) and a._update_info is None
    assert json.loads((tmp_path / "update.json").read_text()) == {"skipped": "v1.4.1"}
    assert not cfg_path.exists(), "skipping must not save the settings"


def test_manual_check_results_are_shown_in_the_status(appmod, root, ui):
    a, opened = ui
    a._on_update_result(None, None, True)
    pump(root, 0.1)
    assert "latest version" in a.status_var.get()
    a._on_update_result(None, OSError("offline"), True)
    pump(root, 0.1)
    assert "Couldn't check for updates" in a.status_var.get()


def test_switch_is_a_normal_setting(appmod, root, ui, cfg_path):
    a, opened = ui
    assert a.update_toggle.cget("text") == "Enabled"
    a.update_toggle.invoke()
    assert a.config["check_updates"] is False and a.update_toggle.cget("text") == "Disabled"
    assert "won't check" in a.update_status.cget("text")
    assert not cfg_path.exists()
    a._save_settings()
    assert json.loads(cfg_path.read_text())["check_updates"] is False


def test_both_bars_fit_together(appmod, root, ui):
    a, opened = ui
    a.on_suspected_mirror(["audioviz.exe"])
    pump(root, 0.1)
    h1 = a.root.winfo_height()
    a._on_update_result(_release(appmod), None, False)
    pump(root, 0.1)
    assert a.root.winfo_height() > h1
    a._hide_mirror_suggestion()
    a._hide_update_bar()
    pump(root, 0.1)
    assert a.root.winfo_height() == a.WINDOW_H
