"""The license files that ship with the app stay in step with what it bundles."""
import re
import sys
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import collect_licenses  # noqa: E402
import update_vlc  # noqa: E402
from vlc_files import VLC_VERSION  # noqa: E402

NOTICES = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")


def _requirements(name):
    names = []
    for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.append(re.split(r"[=<>!~;\[ ]", line)[0].lower())
    return names


def _installed(name):
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return False
    return True


def test_bundled_packages_are_the_requirements_the_spec_keeps():
    spec = (ROOT / "silenceplayer.spec").read_text(encoding="utf-8")
    excluded = re.findall(r'"([^"]+)"', re.search(r"excludes=\[([^\]]*)\]", spec).group(1))
    expected = sorted(set(_requirements("requirements.txt")) - set(excluded))
    assert sorted(collect_licenses.BUNDLED_PACKAGES) == expected, (
        "requirements.txt changed: update BUNDLED_PACKAGES in tools/collect_licenses.py "
        "and the table in THIRD-PARTY-NOTICES.md")


def test_build_tools_with_parts_in_the_app_are_build_requirements():
    assert set(collect_licenses.BUILD_TOOLS) <= set(_requirements("requirements-dev.txt"))


@pytest.mark.parametrize("name", collect_licenses.BUNDLED_PACKAGES + collect_licenses.BUILD_TOOLS)
def test_notices_name_every_bundled_package(name):
    assert f"`{name}`" in NOTICES, f"add {name} to THIRD-PARTY-NOTICES.md"


@pytest.mark.parametrize("doc", update_vlc.DOCS)
def test_docs_name_the_bundled_vlc_version(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert VLC_VERSION in text
    assert set(re.findall(r"\bVLC (\d+\.\d+\.\d+)", text)) <= {VLC_VERSION}, (
        f"{doc} names another VLC version than the bundled {VLC_VERSION}")


def test_update_vlc_writes_the_new_version_everywhere(tmp_path):
    for rel in ["tools/vlc_files.py", *update_vlc.DOCS]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())

    new = ".".join(str(int(part) + 1) for part in VLC_VERSION.split("."))   # 3.0.24 -> 4.1.25

    update_vlc.write_version(VLC_VERSION, new, root=tmp_path)

    assert f'VLC_VERSION = "{new}"' in (tmp_path / "tools" / "vlc_files.py").read_text()
    for doc in update_vlc.DOCS:
        text = (tmp_path / doc).read_text(encoding="utf-8")
        assert new in text and VLC_VERSION not in text, doc


def test_vlc_license_texts_are_in_the_repo():
    gpl = (ROOT / "licenses" / "VLC" / "COPYING.txt").read_text(encoding="utf-8")
    lgpl = (ROOT / "licenses" / "VLC" / "COPYING.LIB.txt").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in gpl and "Version 2, June 1991" in gpl
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in lgpl and "Version 2.1, February 1999" in lgpl


def test_license_files_get_a_name_notepad_opens():
    assert collect_licenses.as_text_name(Path("LICENSE")) == Path("LICENSE.txt")
    assert collect_licenses.as_text_name(Path("COPYING.LGPL")) == Path("COPYING.LGPL.txt")
    assert collect_licenses.as_text_name(Path("LICENSE.md")) == Path("LICENSE.md")
    assert collect_licenses.as_text_name(Path("numpy/ma/LICENSE")) == Path("numpy/ma/LICENSE.txt")


def test_copies_a_packages_license_files(tmp_path):
    folder = collect_licenses.copy_package_licenses("pytest", tmp_path)
    assert folder == tmp_path / f"pytest-{metadata.version('pytest')}"
    assert (folder / "LICENSE.txt").read_text(encoding="utf-8").strip()


def test_fills_the_app_folder(tmp_path):
    missing = [n for n in collect_licenses.BUNDLED_PACKAGES + collect_licenses.BUILD_TOOLS
               if not _installed(n)]
    if missing:
        pytest.skip("not installed here: " + ", ".join(missing))
    app = tmp_path / "SilencePlayer"
    app.mkdir()
    (app / "licenses").mkdir()
    (app / "licenses" / "stale.txt").write_text("from an older build")

    collect_licenses.main(app)

    assert (app / "LICENSE.txt").read_bytes() == (ROOT / "LICENSE").read_bytes()
    notices = (app / "THIRD-PARTY-NOTICES.txt").read_bytes()
    assert notices == (ROOT / "THIRD-PARTY-NOTICES.md").read_bytes()
    assert not (app / "licenses" / "stale.txt").exists()
    assert (app / "licenses" / "VLC" / "COPYING.LIB.txt").is_file()
    assert "PYTHON SOFTWARE FOUNDATION" in (app / "licenses" / "Python" / "LICENSE.txt").read_text(
        encoding="utf-8", errors="replace").upper()
    for name in collect_licenses.BUNDLED_PACKAGES + collect_licenses.BUILD_TOOLS:
        folder = app / "licenses" / f"{name}-{metadata.version(name)}"
        assert any(p.is_file() for p in folder.rglob("*")), name
