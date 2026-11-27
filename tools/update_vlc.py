"""Updates the bundled VLC to another official release, in one command.

    python tools/update_vlc.py 3.0.25            downloads it from VideoLAN
    python tools/update_vlc.py 3.0.25 --zip vlc-3.0.25-win64.zip   uses a file you downloaded

Downloads the official 64-bit zip from download.videolan.org, checks its
SHA-256 against the checksum VideoLAN publishes next to it, then replaces
libvlc.dll, libvlccore.dll and the plugins Silence Player uses (the list in
tools/vlc_files.py). Run the tests and try a few files afterwards.
"""
import argparse
import hashlib
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from vlc_files import vlc_files  # noqa: E402

BASE = "https://download.videolan.org/pub/videolan/vlc/{v}/win64/"


def fetch(url, dest=None):
    req = urllib.request.Request(url, headers={"User-Agent": "SilencePlayer-update-vlc"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        if dest is None:
            return resp.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, length=1 << 20)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("version", help="VLC version, e.g. 3.0.24")
    ap.add_argument("--zip", help="use this already downloaded vlc-<version>-win64.zip")
    args = ap.parse_args()

    name = f"vlc-{args.version}-win64.zip"
    base = BASE.format(v=args.version)
    zip_path = Path(args.zip) if args.zip else ROOT / "build" / name
    if not args.zip:
        zip_path.parent.mkdir(exist_ok=True)
        print(f"Downloading {base}{name} ...")
        fetch(base + name, zip_path)

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    published = fetch(base + name + ".sha256").decode("ascii", "replace").split()[0].lower()
    if digest != published:
        sys.exit(f"Checksum mismatch!\n  file:      {digest}\n  VideoLAN:  {published}")
    print(f"SHA-256 OK ({digest})")

    with zipfile.ZipFile(zip_path) as z:
        index = {n.split("/", 1)[1]: n for n in z.namelist() if "/" in n}
        missing = [rel for rel in vlc_files() if rel not in index]
        if missing:
            sys.exit("This VLC doesn't have: " + ", ".join(missing))
        if (ROOT / "plugins").exists():
            shutil.rmtree(ROOT / "plugins")
        for rel in vlc_files():
            dest = ROOT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(index[rel]) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)

    files = ROOT / "tools" / "vlc_files.py"
    text = re.sub(r'VLC_VERSION = "[^"]*"', f'VLC_VERSION = "{args.version}"',
                  files.read_text(encoding="utf-8"))
    files.write_text(text, encoding="utf-8")
    print(f"Bundled VLC is now {args.version} ({len(vlc_files())} files).")


if __name__ == "__main__":
    main()
