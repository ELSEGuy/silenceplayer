import json
import os
import sys

import pytest

# ── validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, fragment", [
    ("", "empty"),
    ("abc", "not a whole number"),
    ("12.5", "not a whole number"),
    ("0", "out of range"),
    ("3601", "out of range"),
])
def test_parse_int_field_messages(appmod, text, fragment):
    with pytest.raises(appmod.ValidationError) as e:
        appmod.parse_int_field(text, "Silence Timeout", 1, 3600, "seconds")
    msg = str(e.value)
    assert msg, "blank error message"
    assert "Silence Timeout" in msg and "1 and 3600" in msg and fragment in msg


def test_parse_int_field_ok(appmod):
    assert appmod.parse_int_field(" 45 ", "Silence Timeout", 1, 3600) == 45


def test_validation_does_not_use_assert(appmod):
    # Must still reject bad input when Python runs with -O (asserts stripped).
    assert "assert " not in appmod.all_source_code


def test_display_name(appmod, tmp_path):
    f = tmp_path / "asmr" / "Animal Crossing Office.m4a"
    assert appmod.display_name(str(f)) == "Animal Crossing Office.m4a"
    assert appmod.display_name(str(tmp_path / "asmr") + os.sep) == "asmr"
    assert appmod.display_name("") == ""


def test_sanitize_replaces_invalid_values(appmod):
    raw = {"silence_seconds": "abc", "max_volume": 150, "duck_percent": True,
           "mode": "shuffle", "fade_enabled": "yes", "excluded_apps": "discord.exe",
           "future_setting": 42}
    cfg, problems = appmod.sanitize_config(raw)
    d = appmod.DEFAULT_CONFIG
    assert cfg["silence_seconds"] == d["silence_seconds"]
    assert cfg["max_volume"] == d["max_volume"]
    assert cfg["duck_percent"] == d["duck_percent"]
    assert cfg["mode"] == d["mode"]
    assert cfg["fade_enabled"] == d["fade_enabled"]
    assert cfg["excluded_apps"] == []
    assert cfg["future_setting"] == 42            # unknown keys survive
    assert len(problems) == 6


def test_sanitize_accepts_v13_beta_config(appmod):
    legacy = {
        "mp3_path": "C:/Users/me/Music/rain.mp3", "playlist_folder": "",
        "silence_seconds": 30, "max_volume": 80, "fade_enabled": True,
        "duck_percent": 25, "mode": "single", "single_loop_mode": "loop",
        "playlist_loop_mode": "loop_playlist", "excluded_apps": ["Discord.exe"],
        "discord_mirror_fix": False,
    }
    cfg, problems = appmod.sanitize_config(legacy)
    assert problems == []
    assert cfg["excluded_apps"] == ["discord.exe"]
    assert {k: cfg[k] for k in legacy if k != "excluded_apps"} == \
           {k: v for k, v in legacy.items() if k != "excluded_apps"}


def test_sanitize_fills_missing_keys(appmod):
    cfg, problems = appmod.sanitize_config({"mp3_path": "x.mp3"})
    assert cfg["silence_seconds"] == 30 and problems == []


# ── persistence ──────────────────────────────────────────────────────────────

def test_save_and_load_roundtrip(appmod, tmp_path):
    path = str(tmp_path / "config.json")
    cfg, _ = appmod.sanitize_config({"silence_seconds": 12})
    assert appmod.save_config(cfg, path)
    loaded, notice = appmod.load_config(path)
    assert loaded["silence_seconds"] == 12 and notice is None
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_save_failure_is_reported_not_raised(appmod, tmp_path):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x")                      # a file where the folder should be
    path = str(blocker / "config.json")
    assert appmod.save_config({"a": 1}, path) is False


def test_save_does_not_truncate_on_failure(appmod, tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    appmod.save_config({"silence_seconds": 30}, path)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    assert appmod.save_config({"silence_seconds": 99}, path) is False
    with open(path) as f:
        assert json.load(f)["silence_seconds"] == 30
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_corrupt_config_is_backed_up(appmod, tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    cfg, notice = appmod.load_config(str(path))
    assert cfg["silence_seconds"] == 30
    assert notice and "reset" in notice
    backups = [p for p in os.listdir(tmp_path) if p.startswith("config.json.corrupt-")]
    assert len(backups) == 1
    assert (tmp_path / backups[0]).read_text(encoding="utf-8") == "{not json"


def test_missing_config_gives_defaults(appmod, tmp_path):
    cfg, notice = appmod.load_config(str(tmp_path / "nope.json"))
    assert cfg["mode"] == "single" and notice is None


def test_runtime_settings_never_raise(appmod):
    s = appmod.runtime_settings({"silence_seconds": "abc", "max_volume": None,
                                 "duck_percent": float("nan"), "excluded_apps": 5})
    assert s.silence_secs == 30 and s.max_vol == 80 and s.duck_percent == 0
    assert s.excluded == []


# ── ambient source ───────────────────────────────────────────────────────────

def test_validate_ambient_source(appmod, tmp_path, make_config):
    f = tmp_path / "rain.mp3"
    f.write_bytes(b"")
    assert appmod.validate_ambient_source(make_config(mp3_path=str(f))) is None
    assert "choose" in appmod.validate_ambient_source(make_config(mp3_path="")).lower()
    assert "could not be found" in appmod.validate_ambient_source(
        make_config(mp3_path=str(tmp_path / "gone.mp3")))
    assert "folder" in appmod.validate_ambient_source(make_config(mp3_path=str(tmp_path)))

    empty = tmp_path / "empty"
    empty.mkdir()
    assert "supported" in appmod.validate_ambient_source(
        make_config(mode="playlist", playlist_folder=str(empty)))
    assert appmod.validate_ambient_source(
        make_config(mode="playlist", playlist_folder=str(tmp_path))) is None


# ── resume planning ──────────────────────────────────────────────────────────

def _resume(appmod, mode, source, path, index, pos):
    return appmod.ResumeState(mode, source, path, index, pos)


def test_plan_playlist_start(appmod, tmp_path):
    folder = str(tmp_path)
    files = [os.path.join(folder, n) for n in ("a.mp3", "b.mp3", "c.mp3")]
    plan = appmod.plan_playlist_start
    assert plan(folder, files, None) == (0, 0.0)
    assert plan(folder, files, _resume(appmod, "playlist", folder, files[2], 2, 440.0)) == (2, 440.0)
    # saved track gone → its successor, from the start
    gone = os.path.join(folder, "b2.mp3")
    assert plan(folder, files, _resume(appmod, "playlist", folder, gone, 1, 50.0)) == (2, 0.0)
    # saved track was last and is gone → wrap to the first
    gone_last = os.path.join(folder, "z.mp3")
    assert plan(folder, files, _resume(appmod, "playlist", folder, gone_last, 3, 9.0)) == (0, 0.0)
    # different folder or mode → from the top
    assert plan(folder, files, _resume(appmod, "playlist", "/elsewhere", files[2], 2, 4.0)) == (0, 0.0)
    assert plan(folder, files, _resume(appmod, "single", files[2], files[2], 0, 4.0)) == (0, 0.0)


def test_plan_single_start(appmod):
    r = appmod.ResumeState("single", "/m/rain.mp3", "/m/rain.mp3", 0, 763.0)
    assert appmod.plan_single_start("/m/rain.mp3", r) == 763.0
    assert appmod.plan_single_start("/m/forest.mp3", r) == 0.0
    p = appmod.ResumeState("playlist", "/m", "/m/rain.mp3", 0, 763.0)
    assert appmod.plan_single_start("/m/rain.mp3", p) == 0.0


# ── single instance & logging ────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_single_instance_noop_off_windows(appmod):
    assert appmod.SingleInstance().acquire() is True


@pytest.mark.skipif(sys.platform != "win32", reason="needs the Windows kernel mutex")
def test_single_instance_blocks_second(appmod, monkeypatch):
    monkeypatch.setattr(appmod.SingleInstance, "MUTEX_NAME", "Local\\SilencePlayer.Test.Mutex")
    monkeypatch.setattr(appmod.SingleInstance, "EVENT_NAME", "Local\\SilencePlayer.Test.Event")
    first = appmod.SingleInstance()
    assert first.acquire() is True
    shown = []
    first.listen(lambda: shown.append(1))
    second = appmod.SingleInstance()
    assert second.acquire() is False
    import time
    deadline = time.monotonic() + 2
    while not shown and time.monotonic() < deadline:
        time.sleep(0.02)
    assert shown, "first instance was not asked to show Settings"
    first.release()
    third = appmod.SingleInstance()
    assert third.acquire() is True      # released → no stale lock
    third.release()


def test_log_throttled_suppresses_repeats(appmod, caplog):
    import logging
    appmod.log.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="silenceplayer"):
            for _ in range(50):
                appmod.log_throttled("test-key", logging.WARNING, "pycaw failed")
        assert len([r for r in caplog.records if "pycaw failed" in r.getMessage()]) == 1
    finally:
        appmod.log.propagate = False


def test_throttle_filter_only_affects_proctap(appmod):
    import logging
    f = appmod._ThrottleFilter("proctap", 60)
    mk = lambda name: logging.LogRecord(name, logging.ERROR, __file__, 1, "Error reading", (), None)
    assert f.filter(mk("proctap.core")) is True
    assert f.filter(mk("proctap.core")) is False
    assert f.filter(mk("silenceplayer")) is True
    assert f.filter(mk("silenceplayer")) is True


def test_setup_logging_survives_unwritable_dir(appmod, monkeypatch, tmp_path):
    blocker = tmp_path / "file_not_dir"
    blocker.write_text("x")
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))
    monkeypatch.setattr(appmod.setup_logging, "_done", False, raising=False)
    old_handlers = list(appmod.log.handlers)
    try:
        appmod.setup_logging()           # must not raise
        appmod.log.error("still fine")
    finally:
        for h in list(appmod.log.handlers):
            if h not in old_handlers:
                appmod.log.removeHandler(h)


# ── Mirroring Fix config ─────────────────────────────────────────────────────

def test_mirror_fix_defaults_on_with_known_apps(appmod):
    cfg, _ = appmod.sanitize_config({})
    assert cfg["mirror_fix_enabled"] is True
    assert appmod.enabled_mirror_apps(cfg) >= {"discord.exe", "signalrgb.exe",
                                               "wallpaper64.exe", "wallpaper32.exe",
                                               "obs64.exe", "streamlabs desktop.exe",
                                               "medal.exe"}
    cfg2, _ = appmod.sanitize_config({})
    cfg2["mirror_apps"].append({"name": "x.exe", "enabled": True})
    assert "x.exe" not in appmod.enabled_mirror_apps(cfg)   # defaults not shared


def test_beta_config_gets_mirror_fix(appmod):
    cfg, problems = appmod.sanitize_config({"mp3_path": "a.mp3", "discord_mirror_fix": False})
    assert problems == []
    assert cfg["mirror_fix_enabled"] is True and "signalrgb.exe" in appmod.enabled_mirror_apps(cfg)


def test_mirror_apps_normalized(appmod):
    cfg, _ = appmod.sanitize_config({"mirror_apps": [
        "SignalRGB", {"name": "Discord.exe", "enabled": False},
        {"name": "signalrgb.exe"}, 42, {"enabled": True}]})
    assert cfg["mirror_apps"][:2] == [{"name": "signalrgb.exe", "enabled": True},
                                      {"name": "discord.exe", "enabled": False}]
    enabled = appmod.enabled_mirror_apps(cfg)
    assert "signalrgb.exe" in enabled and "discord.exe" not in enabled


def test_normalize_app_name(appmod):
    assert appmod.normalize_app_name(' "SignalRgb" ') == "signalrgb.exe"
    assert appmod.normalize_app_name("wallpaper64.EXE") == "wallpaper64.exe"
    assert appmod.normalize_app_name("  ") == ""


def test_new_mirror_defaults_added_once(appmod):
    # A config saved before v2 (no version key), where the user removed Wallpaper Engine.
    old = {"mirror_apps": [{"name": "discord.exe", "enabled": False},
                           {"name": "signalrgb.exe", "enabled": True}]}
    cfg, problems = appmod.sanitize_config(old)
    names = [a["name"] for a in cfg["mirror_apps"]]
    assert problems == []
    assert "obs64.exe" in names and "medal.exe" in names        # new defaults added
    assert "wallpaper64.exe" not in names                       # removed one stays removed
    assert cfg["mirror_apps"][0] == {"name": "discord.exe", "enabled": False}
    assert cfg["mirror_defaults_version"] == appmod.MIRROR_DEFAULTS_VERSION
    # Saved again and reloaded: nothing re-added after the user removes OBS.
    cfg["mirror_apps"] = [a for a in cfg["mirror_apps"] if a["name"] != "obs64.exe"]
    again, _ = appmod.sanitize_config(cfg)
    assert "obs64.exe" not in [a["name"] for a in again["mirror_apps"]]
