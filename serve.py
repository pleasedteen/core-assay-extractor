#!/usr/bin/env python3 -I
"""Launch the corebench workbench web UI.

Usage:
    python3 -I serve.py [--data-dir DIR] [--host 127.0.0.1] [--port 8077]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corebench.webapp import main  # noqa: E402

if __name__ == "__main__":
    # Re-parse arguments inside the module's main().
    sys.argv[0] = "corebench.webapp"
    main()
