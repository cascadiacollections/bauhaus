"""Fail CI when generation benchmark metrics exceed configured thresholds."""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate bauhaus benchmark metrics thresholds")
    parser.add_argument("--metrics", required=True, help="Path to metrics JSON produced by src/main.py")
    parser.add_argument("--max-total", type=float, required=True, help="Max allowed total runtime (seconds)")
    parser.add_argument("--max-style-transfer", type=float, required=True,
                        help="Max allowed style transfer runtime (seconds)")
    args = parser.parse_args()

    metrics_path = Path(args.metrics)
    if not metrics_path.exists():
        print(f"Metrics file not found: {metrics_path}", file=sys.stderr)
        return 1

    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    total = float(payload.get("total_sec", 0.0))
    style_transfer = float(payload.get("timings_sec", {}).get("style_transfer", 0.0))

    print(f"Benchmark metrics: total_sec={total:.3f}, style_transfer_sec={style_transfer:.3f}")

    errors: list[str] = []
    if total > args.max_total:
        errors.append(f"total_sec {total:.3f} exceeded max_total {args.max_total:.3f}")
    if style_transfer > args.max_style_transfer:
        errors.append(
            f"style_transfer {style_transfer:.3f} exceeded "
            f"max_style_transfer {args.max_style_transfer:.3f}"
        )

    if errors:
        print("Benchmark gate failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Benchmark gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
