"""Launcher: installs the add-on, then runs the UNCHANGED server.py as __main__.

    cd voice_agent && uv run python -m observability

Equivalent to `python server.py`, plus the hooks in observability/bootstrap.py.
"""

import runpy
import sys

from .bootstrap import APP_DIR, install
from .shutdown import flush_all


def main() -> None:
  install()
  server = APP_DIR / "server.py"
  sys.argv[0] = str(server)
  try:
    runpy.run_path(str(server), run_name="__main__")
  finally:
    flush_all()  # last flush after the server's own cleanup


if __name__ == "__main__":
  main()
