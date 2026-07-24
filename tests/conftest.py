"""Make the repository root importable for every test module.

`pytest tests/` (as CI runs it) does not put the working directory on
sys.path, so `from inmermusic import ...` fails without this. test_features.py
keeps its own sys.path line so it still runs standalone via
`python tests/test_features.py`, which never loads this file.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
