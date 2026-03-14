"""Shared fixtures for bauhaus tests."""

import sys
from pathlib import Path

# Add src/ to sys.path so tests can import source modules directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
