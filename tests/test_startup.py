import json
import os
import sys

import pytest


class DictRegistry:
    """Stands in for HKEY_CURRENT_USER."""

    def __init__(self):
        self.store = {}
        self.fail = False

    def get(self, path, name):
        return self.store.get((path, name))

    def set(self, path, name, value):
        if self.fail:
            raise PermissionError("registry is read-only")
        self.store[(path, name)] = value

    def delete(self, path, name):
        if self.fail:
            raise PermissionError("registry is read-only")
        self.store.pop((path, name), None)


@pytest.fixture
def reg():
    return DictRegistry()


@pytest.fixture
def entry(appmod, reg):
    return appmod.StartupEntry(registry=reg, command=lambda: r'"C:\Apps\silenceplayer.exe"')


def run_key(appmod):
    return appmod.StartupEntry.RUN_KEY, appmod.StartupEntry.VALUE_NAME


def approved_key(appmod):
    return appmod.StartupEntry.APPROVED_KEY, appmod.StartupEntry.VALUE_NAME


# ── the registry entry ───────────────────────────────────────────────────────

def test_off_by_default(entry):
    assert entry.available and entry.status() == "off" and not entry.is_enabled()


def test_enable_writes_the_run_entry_and_disable_removes_it(appmod, entry, reg):
    entry.set_enabled(True)
    assert reg.store[run_key(appmod)] == r'"C:\Apps\silenceplayer.exe"'
    assert entry.status() == "on" and entry.is_enabled()
    entry.set_enabled(False)
    assert run_key(appmod) not in reg.store
    assert entry.status() == "off"


def test_disabling_when_already_off_is_harmless(entry):
    entry.set_enabled(False)
    entry.set_enabled(False)
    assert entry.status() == "off"


def test_windows_can_switch_it_off_and_we_respect_that(appmod, entry, reg):
    entry.set_enabled(True)
    reg.store[approved_key(appmod)] = bytes([3]) + bytes(11)      # off in Task Manager
    assert entry.status() == "blocked" and not entry.is_enabled()


def test_windows_approved_marker_means_on(appmod, entry, reg):
    entry.set_enabled(True)
    reg.store[approved_key(appmod)] = bytes([2]) + bytes(11)
    assert entry.status() == "on"


def test_turning_it_on_again_clears_the_windows_off_switch(appmod, entry, reg):
    entry.set_enabled(True)
    reg.store[approved_key(appmod)] = bytes([3]) + bytes(11)
    entry.set_enabled(True)
    assert approved_key(appmod) not in reg.store
    assert entry.status() == "on"


def test_unavailable_without_winreg(appmod, monkeypatch):
    def no_winreg():
        raise ImportError("winreg")

    monkeypatch.setattr(appmod, "_WinRegistry", no_winreg)
    entry = appmod.StartupEntry()
    assert not entry.available and entry.status() == "unavailable"
    assert not entry.is_enabled()
    with pytest.raises(OSError):
        entry.set_enabled(True)


def test_registry_failure_is_reported_to_the_caller(entry, reg):
    reg.fail = True
    with pytest.raises(PermissionError):
        entry.set_enabled(True)


# ── the command line ─────────────────────────────────────────────────────────

def test_packaged_exe_command_is_just_the_exe(appmod, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", r"C:\Temp\_MEI123", raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Users\me\SilencePlayer\silenceplayer.exe")
    assert appmod.startup_command() == r'"C:\Users\me\SilencePlayer\silenceplayer.exe"'


def test_source_command_uses_pythonw_and_the_full_path_of_app_py(appmod, monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    cmd = appmod.startup_command()
    assert cmd.startswith(f'"{tmp_path / "pythonw.exe"}"')
    assert cmd.endswith(f'"{appmod.LAUNCHER}"')
    assert appmod.LAUNCHER.endswith("app.py") and os.path.isfile(appmod.LAUNCHER)


def test_source_command_without_pythonw_keeps_the_interpreter(appmod, monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert appmod.startup_command().startswith(f'"{tmp_path / "python.exe"}"')


# ── keeping the entry working after the EXE moves ────────────────────────────

def test_moved_exe_updates_the_entry(appmod, entry, reg, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", r"C:\Temp\_MEI1", raising=False)
    reg.store[run_key(appmod)] = r'"D:\Old\silenceplayer.exe"'
    assert entry.repair_path() is True
    assert reg.store[run_key(appmod)] == r'"C:\Apps\silenceplayer.exe"'
    assert entry.repair_path() is False                      # already right


def test_repair_does_nothing_when_off_or_running_from_source(appmod, entry, reg, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", r"C:\Temp\_MEI1", raising=False)
    assert entry.repair_path() is False and run_key(appmod) not in reg.store
    reg.store[run_key(appmod)] = r'"D:\Old\silenceplayer.exe"'
    monkeypatch.delattr(sys, "_MEIPASS")
    assert entry.repair_path() is False                      # a source run never rewrites it
    assert reg.store[run_key(appmod)] == r'"D:\Old\silenceplayer.exe"'


def test_repair_leaves_a_windows_off_switch_alone(appmod, entry, reg, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", r"C:\Temp\_MEI1", raising=False)
    reg.store[run_key(appmod)] = r'"D:\Old\silenceplayer.exe"'
    reg.store[approved_key(appmod)] = bytes([3]) + bytes(11)
    entry.repair_path()
    assert reg.store[approved_key(appmod)][0] == 3


# ── Mirroring Fix defaults added in 1.4.0 ────────────────────────────────────

NEW_APPS = {"xsplit.core.exe", "xsplit.gamecaster.exe", "lively.exe",
            "lively.playerwebview2.exe", "lively.playercefsharp.exe", "fluentflyout.exe"}


def test_new_installs_include_xsplit_lively_and_fluentflyout(appmod):
    cfg, _ = appmod.sanitize_config({})
    assert appmod.enabled_mirror_apps(cfg) >= NEW_APPS


def test_existing_configs_get_them_added_once(appmod):
    old = {"mirror_defaults_version": 2,
           "mirror_apps": [{"name": "discord.exe", "enabled": True},
                           {"name": "lively.exe", "enabled": False}]}      # user turned one off
    cfg, problems = appmod.sanitize_config(old)
    assert problems == []
    by_name = {a["name"]: a["enabled"] for a in cfg["mirror_apps"]}
    assert NEW_APPS <= set(by_name)
    assert by_name["lively.exe"] is False                  # their choice is kept
    assert by_name["xsplit.core.exe"] is True
    cfg["mirror_apps"] = [a for a in cfg["mirror_apps"] if a["name"] != "fluentflyout.exe"]
    again, _ = appmod.sanitize_config(cfg)
    assert "fluentflyout.exe" not in [a["name"] for a in again["mirror_apps"]]   # not re-added
