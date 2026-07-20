"""Tests for benchmark_gate.py — regression safeguards for benchmark thresholds."""

import json
import subprocess
import sys
from pathlib import Path


def test_benchmark_gate_passes_when_within_threshold(tmp_path):
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({
        "total_sec": 2.5,
        "timings_sec": {"style_transfer": 0.8},
    }), encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "src" / "benchmark_gate.py"
    exit_code = subprocess.run([
        sys.executable,
        str(script),
        "--metrics",
        str(metrics),
        "--max-total",
        "3.0",
        "--max-style-transfer",
        "1.0",
    ], capture_output=True, text=True, check=False)

    assert exit_code.returncode == 0
    assert "Benchmark gate passed." in exit_code.stdout


def test_benchmark_gate_fails_when_threshold_exceeded(tmp_path):
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({
        "total_sec": 4.0,
        "timings_sec": {"style_transfer": 1.2},
    }), encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "src" / "benchmark_gate.py"
    exit_code = subprocess.run([
        sys.executable,
        str(script),
        "--metrics",
        str(metrics),
        "--max-total",
        "3.0",
        "--max-style-transfer",
        "1.0",
    ], capture_output=True, text=True, check=False)

    assert exit_code.returncode == 1
    assert "Benchmark gate failed:" in exit_code.stderr


def test_benchmark_gate_reports_missing_file(tmp_path):
    missing = tmp_path / "missing.json"

    script = Path(__file__).resolve().parents[1] / "src" / "benchmark_gate.py"
    exit_code = subprocess.run([
        sys.executable,
        str(script),
        "--metrics",
        str(missing),
        "--max-total",
        "10.0",
        "--max-style-transfer",
        "10.0",
    ], capture_output=True, text=True, check=False)

    assert exit_code.returncode == 1
    assert "Metrics file not found" in exit_code.stderr
