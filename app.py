"""Starts Silence Player (run.bat, `python app.py`, and the EXE build use this).

The code lives in the silenceplayer/ folder.
"""
import sys

from silenceplayer.main import main

if __name__ == "__main__":
    sys.exit(main())
