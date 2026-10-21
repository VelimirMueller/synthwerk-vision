"""Evaluate the configured backend on the frozen eval set (eval/manifest.yaml).

Prints a markdown report, writes eval/reports/<model>.json, and exits 1 when root-topic
accuracy is below --min-topic-accuracy or fewer than 90 % of the images are available.
Uses the same SYNTHWERK_VISION_* settings as the server (e.g. SYNTHWERK_VISION_BACKEND=mobilenet).

Usage: uv run python scripts/evaluate.py [--min-topic-accuracy 0.8]
"""

import argparse
import json
import sys
from pathlib import Path

from synthwerk_vision.evaluation import evaluate, load_manifest
from synthwerk_vision.main import build_service
from synthwerk_vision.platform.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
MIN_AVAILABLE = 0.9


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "eval" / "manifest.yaml")
    parser.add_argument("--images", type=Path, default=ROOT / "eval" / "images")
    parser.add_argument("--reports", type=Path, default=ROOT / "eval" / "reports")
    parser.add_argument("--min-topic-accuracy", type=float, default=0.0)
    args = parser.parse_args()

    service = build_service(get_settings())
    if service is None:
        print("model not available; run scripts/fetch_model.py", file=sys.stderr)
        return 2
    report = evaluate(service, load_manifest(args.manifest), args.images)

    args.reports.mkdir(parents=True, exist_ok=True)
    out = args.reports / f"{report.model}.json"
    out.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    print(report.markdown())
    print(f"report: {out.relative_to(ROOT)}")

    failures = []
    if report.available_ratio < MIN_AVAILABLE:
        failures.append(f"only {report.scored}/{report.images} images available")
    if report.topic_accuracy < args.min_topic_accuracy:
        failures.append(
            f"root-topic accuracy {report.topic_accuracy:.1%} < {args.min_topic_accuracy:.1%}"
        )
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
