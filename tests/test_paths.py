import json
import os


def test_config_stays_next_to_the_app_when_possible(appmod, tmp_path):
    app_dir = tmp_path / "SilencePlayer"
    app_dir.mkdir()
    assert appmod.choose_config_file(str(app_dir), str(tmp_path / "AppData")) == \
        str(app_dir / "config.json")


def test_read_only_folder_uses_appdata_and_keeps_settings(appmod, tmp_path, monkeypatch):
    app_dir = tmp_path / "Program Files" / "SilencePlayer"
    app_dir.mkdir(parents=True)
    (app_dir / "config.json").write_text(json.dumps({"silence_seconds": 42}))
    monkeypatch.setattr(appmod, "is_writable", lambda folder: folder != str(app_dir))
    chosen = appmod.choose_config_file(str(app_dir), str(tmp_path / "AppData"))
    assert chosen == str(tmp_path / "AppData" / "config.json")
    assert json.loads(open(chosen).read()) == {"silence_seconds": 42}   # copied over once
    (tmp_path / "AppData" / "config.json").write_text("{}")              # later changes...
    appmod.choose_config_file(str(app_dir), str(tmp_path / "AppData"))
    assert open(chosen).read() == "{}"                                  # ...are never overwritten


def test_is_writable(appmod, tmp_path):
    assert appmod.is_writable(str(tmp_path))
    assert not appmod.is_writable(str(tmp_path / "does-not-exist"))
    assert os.listdir(tmp_path) == []                 # the probe file is cleaned up


def test_save_config_creates_its_folder(appmod, tmp_path):
    target = tmp_path / "AppData" / "SilencePlayer" / "config.json"
    assert appmod.save_config({"a": 1}, str(target))
    assert json.loads(target.read_text()) == {"a": 1}


def test_bundle_and_launcher_paths(appmod):
    assert os.path.isfile(appmod.LAUNCHER)
    assert os.path.isfile(appmod.resource_path("icon.png"))
    assert os.path.isfile(os.path.join(appmod.BUNDLE_DIR, "libvlc.dll"))
