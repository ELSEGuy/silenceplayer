"""Every file that names the bundled VLC version names the same one."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import update_vlc  # noqa: E402
from vlc_files import VLC_VERSION  # noqa: E402


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
