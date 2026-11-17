#!/usr/bin/env python3 -I
"""Test entry point.

Run with ``python3 -I run_tests.py``: isolated mode keeps the interpreter
ignoring environment/cwd side effects, while this script explicitly puts the
project root on ``sys.path`` (the project itself is trusted code).
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"), pattern="test_*.py",
                            top_level_dir=ROOT)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
