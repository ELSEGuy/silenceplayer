"""Audio sessions on every output device, process names, COM setup and the
gentle ProcTap reader. The Windows APIs are replaced by small fakes."""
import sys
import threading
import types

import pytest
from conftest import wait_until

# ── fake COM objects for _active_session_managers / list_sessions ────────────

class FakeCtl:
    def __init__(self, pid):
        self.pid = pid

    def QueryInterface(self, iface):
        return self


class FakeSessionEnum:
    def __init__(self, pids):
        self.pids = pids

    def GetCount(self):
        return len(self.pids)

    def GetSession(self, i):
        return FakeCtl(self.pids[i])


class FakeManager:
    def __init__(self, pids, broken=False):
        self.pids, self.broken = pids, broken

    def GetSessionEnumerator(self):
        if self.broken:
            raise OSError("device invalidated")
        return FakeSessionEnum(self.pids)

    def QueryInterface(self, iface):
        return self


class FakeDevice:
    def __init__(self, manager, activate_fails=False):
        self.manager, self.activate_fails = manager, activate_fails

    def Activate(self, iid, ctx, params):
        if self.activate_fails:
            raise OSError("cannot activate")
        return self.manager


class FakeWindows:
    """Installs fake comtypes / pycaw.constants / pycaw.pycaw pieces."""

    def __init__(self, monkeypatch, devices):
        self.devices = devices
        self.enumerations = 0
        win = self

        class Collection:
            def GetCount(self):
                return len(win.devices)

            def Item(self, i):
                return win.devices[i]

        class Enumerator:
            def EnumAudioEndpoints(self, flow, state):
                win.enumerations += 1
                return Collection()

        comtypes = types.ModuleType("comtypes")
        comtypes.CLSCTX_INPROC_SERVER = comtypes.CLSCTX_ALL = 0
        comtypes.CoCreateInstance = lambda *a: Enumerator()
        consts = types.ModuleType("pycaw.constants")
        consts.CLSID_MMDeviceEnumerator = object()
        consts.DEVICE_STATE = types.SimpleNamespace(ACTIVE=types.SimpleNamespace(value=1))
        consts.EDataFlow = types.SimpleNamespace(eRender=types.SimpleNamespace(value=0))
        monkeypatch.setitem(sys.modules, "comtypes", comtypes)
        monkeypatch.setitem(sys.modules, "pycaw.constants", consts)
        pycaw_mod = sys.modules["pycaw.pycaw"]

        class AudioSession:
            def __init__(self, ctl):
                self._ctl = ctl
                self.ProcessId = ctl.pid

        monkeypatch.setattr(pycaw_mod, "AudioSession", AudioSession, raising=False)
        monkeypatch.setattr(pycaw_mod, "IAudioSessionControl2", object(), raising=False)
        monkeypatch.setattr(pycaw_mod, "IAudioSessionManager2",
                            types.SimpleNamespace(_iid_=object()), raising=False)
        monkeypatch.setattr(pycaw_mod, "IMMDeviceEnumerator", object(), raising=False)


@pytest.fixture
def fresh_cache(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "_device_cache", appmod._DeviceCache())


def test_sessions_come_from_every_output_device(appmod, monkeypatch, fresh_cache):
    FakeWindows(monkeypatch, [FakeDevice(FakeManager([10, 11])),      # speakers
                              FakeDevice(FakeManager([20]))])         # headset
    assert sorted(s.ProcessId for s in appmod.list_sessions()) == [10, 11, 20]


def test_a_device_that_cannot_be_opened_is_skipped(appmod, monkeypatch, fresh_cache):
    FakeWindows(monkeypatch, [FakeDevice(FakeManager([10]), activate_fails=True),
                              FakeDevice(FakeManager([20]))])
    assert [s.ProcessId for s in appmod.list_sessions()] == [20]


def test_no_usable_device_is_an_error_not_silence(appmod, monkeypatch, fresh_cache):
    FakeWindows(monkeypatch, [FakeDevice(FakeManager([10]), activate_fails=True)])
    with pytest.raises(OSError):
        appmod.list_sessions()


def test_device_list_is_cached_then_refreshed(appmod, monkeypatch, fresh_cache):
    clock = {"t": 1000.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["t"])
    win = FakeWindows(monkeypatch, [FakeDevice(FakeManager([10]))])
    appmod.list_sessions()
    appmod.list_sessions()
    assert win.enumerations == 1                       # cached
    win.devices.append(FakeDevice(FakeManager([30])))  # headset plugged in
    clock["t"] += appmod.DEVICE_REFRESH_SECONDS + 0.1
    assert sorted(s.ProcessId for s in appmod.list_sessions()) == [10, 30]
    assert win.enumerations == 2


def test_a_vanished_device_triggers_a_fresh_list(appmod, monkeypatch, fresh_cache):
    broken = FakeManager([10], broken=True)
    win = FakeWindows(monkeypatch, [FakeDevice(broken)])
    with pytest.raises(OSError):
        appmod.list_sessions()                     # the error reaches the caller...
    broken.broken = False
    appmod.list_sessions()
    assert win.enumerations == 2                   # ...and the devices are listed again


def test_device_cache_is_per_thread(appmod, monkeypatch, fresh_cache):
    win = FakeWindows(monkeypatch, [FakeDevice(FakeManager([10]))])
    appmod.list_sessions()
    t = threading.Thread(target=appmod.list_sessions)
    t.start()
    t.join()
    assert win.enumerations == 2                   # COM objects stay on their own thread


# ── process names ────────────────────────────────────────────────────────────

def test_process_names_are_cached(appmod):
    calls = []
    clock = {"t": 0.0}

    def lookup(pid):
        calls.append(pid)
        return "Discord.exe"

    names = appmod.ProcessNames(lookup=lookup, clock=lambda: clock["t"])
    assert names.get(5) == "discord.exe"
    assert names.get(5) == "discord.exe"
    assert calls == [5]
    clock["t"] += names.TTL + 1                    # looked up again after a while
    names.get(5)
    assert calls == [5, 5]


def test_process_names_forget_exited_processes(appmod):
    calls = []
    names = appmod.ProcessNames(lookup=lambda pid: calls.append(pid) or "a.exe")
    names.get(1)
    names.get(2)
    names.forget_missing({2})
    names.get(1)
    names.get(2)
    assert calls == [1, 2, 1]


def test_process_name_lookup_failure_is_none(appmod):
    def boom(pid):
        raise ProcessLookupError(pid)

    assert appmod.ProcessNames(lookup=boom).get(7) is None


# ── COM ──────────────────────────────────────────────────────────────────────

def test_ensure_mta_does_nothing_off_windows(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "_MTA_COOKIE", None)
    monkeypatch.setattr(appmod.sys, "platform", "linux")
    assert appmod.ensure_mta() is False


def test_ensure_mta_keeps_the_apartment_open_once(appmod, monkeypatch):
    import ctypes
    calls = []

    class Ole32:
        def CoIncrementMTAUsage(self, cookie_ref):
            calls.append(cookie_ref)
            return 0

    monkeypatch.setattr(appmod, "_MTA_COOKIE", None)
    monkeypatch.setattr(appmod.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "oledll", types.SimpleNamespace(ole32=Ole32()), raising=False)
    assert appmod.ensure_mta() is True
    assert appmod.ensure_mta() is True                 # only once
    assert len(calls) == 1


def test_ensure_mta_failure_is_not_fatal(appmod, monkeypatch):
    import ctypes

    class Ole32:
        def CoIncrementMTAUsage(self, cookie_ref):
            raise OSError("E_FAIL")

    monkeypatch.setattr(appmod, "_MTA_COOKIE", None)
    monkeypatch.setattr(appmod.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "oledll", types.SimpleNamespace(ole32=Ole32()), raising=False)
    assert appmod.ensure_mta() is False


# ── the gentle ProcTap reader ────────────────────────────────────────────────

class FakeBackend:
    def __init__(self):
        self.queue = []
        self.reads = 0
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            self.reads += 1
            return self.queue.pop(0) if self.queue else None


def _quiet_capture(appmod, backend, callback):
    cap = object.__new__(appmod.QuietCapture)          # skip ProcTap's real __init__
    cap._backend = backend
    cap._stop_event = threading.Event()
    cap._on_data = callback
    return cap


def test_quiet_reader_drains_the_buffer_in_one_go(appmod):
    got = []
    backend = FakeBackend()
    backend.queue = [b"a" * 8, b"b" * 8, b"c" * 8]
    cap = _quiet_capture(appmod, backend, lambda data, frames: got.append(data))
    t = threading.Thread(target=cap._worker)
    t.start()
    assert wait_until(lambda: got)
    cap._stop_event.set()
    t.join(2)
    assert got[0] == b"a" * 8 + b"b" * 8 + b"c" * 8    # one callback for everything queued


def test_quiet_reader_sleeps_between_reads(appmod, monkeypatch):
    monkeypatch.setattr(appmod.QuietCapture, "POLL_SECONDS", 0.05)
    backend = FakeBackend()
    cap = _quiet_capture(appmod, backend, lambda data, frames: None)
    t = threading.Thread(target=cap._worker)
    t.start()
    threading.Event().wait(0.5)
    cap._stop_event.set()
    t.join(2)
    assert not t.is_alive()
    assert backend.reads <= 15                         # ProcTap's own loop would do ~500


def test_quiet_reader_survives_errors(appmod):
    got = []

    class Flaky(FakeBackend):
        def read(self):
            self.reads += 1
            if self.reads == 1:
                raise RuntimeError("glitch")
            return b"x" * 8 if self.reads == 2 else None

    def callback(data, frames):
        got.append(data)
        raise ValueError("callback bug")               # must not kill the reader

    backend = Flaky()
    cap = _quiet_capture(appmod, backend, callback)
    t = threading.Thread(target=cap._worker)
    t.start()
    assert wait_until(lambda: got)
    assert wait_until(lambda: backend.reads > 4)
    cap._stop_event.set()
    t.join(2)
    assert not t.is_alive()


def test_mirror_fix_uses_the_quiet_reader_by_default(appmod):
    fix = appmod.MirrorFix(apps_source=lambda: set())
    assert fix._capture_factory is appmod.QuietCapture
