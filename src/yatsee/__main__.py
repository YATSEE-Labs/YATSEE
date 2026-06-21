"""
Executable package entrypoint for YATSEE.

This module allows the package to be run with:

    python -m yatsee

The real CLI implementation lives in yatsee.cli.main so command registration
and package execution stay separated.

:return: Process exit code through sys.exit
"""

from __future__ import annotations

import sys

from yatsee.cli.main import main


if __name__ == "__main__":
    sys.exit(main())