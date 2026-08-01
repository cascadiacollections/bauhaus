"""Shared fixtures for bauhaus tests."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# Add src/ to sys.path so tests can import source modules directly, and this
# directory so tests can import helpers.py.
sys.path.insert(0, str(_ROOT.parent / "src"))
sys.path.insert(0, str(_ROOT))
