"""Prints the CHANGELOG.md section of one version (used for draft releases).

    python tools/release_notes.py 1.4.0
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(text, version):
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lstrip("v").split()[0] == version
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def main():
    version = sys.argv[1].lstrip("v")
    notes = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    print(notes or f"Silence Player {version}. See CHANGELOG.md for details.")
    print("\nDownload **SilencePlayer-Setup** to install, or the **portable** zip to run "
          "it from any folder.")


if __name__ == "__main__":
    main()
