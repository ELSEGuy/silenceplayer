"""Puts the license files next to the built app (build.ps1 runs this after PyInstaller).

    python tools/collect_licenses.py dist/SilencePlayer

Writes into that folder:
    LICENSE.txt                    Silence Player's own license
    THIRD-PARTY-NOTICES.txt        what else is in the app, and under which license
    licenses/VLC/                  VLC's license texts (from licenses/VLC in this repo)
    licenses/Python/LICENSE.txt    Python's own license file
    licenses/<package>-<version>/  the license files of each bundled package

Run it with the Python that made the build: the package license files come
from its environment, so they always match the versions built into the app.
"""
import shutil
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The packages that end up inside the app: requirements.txt without scipy
# (silenceplayer.spec excludes it). tests/test_licenses.py checks this list
# against requirements.txt and THIRD-PARTY-NOTICES.md.
BUNDLED_PACKAGES = ["comtypes", "numpy", "pillow", "proc-tap", "psutil", "pycaw",
                    "pystray", "python-vlc", "six"]
# Build tools with parts inside the app: PyInstaller's bootloader is silenceplayer.exe.
BUILD_TOOLS = ["pyinstaller"]

LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
TEXT_SUFFIXES = {".txt", ".md", ".rst"}


def license_files(dist):
    """(file, path inside the package's folder) for each license file a package installed."""
    found = []
    for f in dist.files or []:
        parts = f.parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        if parts[1] == "licenses" and len(parts) > 2:      # where newer packages put them
            rel = Path(*parts[2:])
        elif len(parts) == 2 and parts[1].upper().startswith(LICENSE_NAMES):
            rel = Path(parts[1])
        else:
            continue
        found.append((Path(dist.locate_file(f)), rel))
    return found


def as_text_name(rel):
    """LICENSE -> LICENSE.txt, so Windows opens it in Notepad."""
    if rel.suffix.lower() in TEXT_SUFFIXES:
        return rel
    return rel.with_name(rel.name + ".txt")


def copy_package_licenses(name, licenses_dir):
    """Copies one package's license files into licenses_dir/<name>-<version>."""
    dist = metadata.distribution(name)
    files = license_files(dist)
    if not files:
        raise SystemExit(f"{name} {dist.version} has no license file in its metadata")
    folder = Path(licenses_dir) / f"{name}-{dist.version}"
    for src, rel in files:
        dest = folder / as_text_name(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    return folder


def python_license():
    """Python's LICENSE.txt: in the install folder on Windows, in lib/pythonX.Y elsewhere."""
    base = Path(sys.base_prefix)
    lib = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for candidate in (base / "LICENSE.txt", base / "lib" / lib / "LICENSE.txt"):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"Python's LICENSE.txt is missing from {base}. "
                     "Build with Python from python.org.")


def main(app_dir):
    app_dir = Path(app_dir)
    if not app_dir.is_dir():
        raise SystemExit(f"No app folder at {app_dir}")
    licenses_dir = app_dir / "licenses"
    if licenses_dir.exists():
        shutil.rmtree(licenses_dir)

    shutil.copyfile(ROOT / "LICENSE", app_dir / "LICENSE.txt")
    shutil.copyfile(ROOT / "THIRD-PARTY-NOTICES.md", app_dir / "THIRD-PARTY-NOTICES.txt")
    shutil.copytree(ROOT / "licenses", licenses_dir)
    (licenses_dir / "Python").mkdir()
    shutil.copyfile(python_license(), licenses_dir / "Python" / "LICENSE.txt")
    for name in BUNDLED_PACKAGES + BUILD_TOOLS:
        copy_package_licenses(name, licenses_dir)

    count = sum(1 for p in licenses_dir.rglob("*") if p.is_file())
    print(f"License files: LICENSE.txt, THIRD-PARTY-NOTICES.txt and {count} in licenses/")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
