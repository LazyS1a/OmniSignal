"""Command-line entry point for reverse-lab gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .gate import LabGateError, LabWorkspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OmniSignal authorized reverse-lab gate")
    parser.add_argument("--workspace", type=Path, default=Path("reverse_lab"))
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify-sample", help="verify allowlist, path and SHA-256")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--actor", required=True)

    promote = subparsers.add_parser("approve-promotion", help="verify an approved connector promotion review")
    promote.add_argument("--manifest", type=Path, required=True)
    promote.add_argument("--finding", type=Path, required=True)
    promote.add_argument("--review", type=Path, required=True)
    promote.add_argument("--actor", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = LabWorkspace(args.workspace)
    try:
        if args.command == "verify-sample":
            verified = workspace.verify_sample(args.manifest, actor=args.actor)
            print(json.dumps({"status": "allowed", **verified.__dict__}, ensure_ascii=False, sort_keys=True))
        else:
            review = workspace.approve_promotion(
                manifest_path=args.manifest,
                finding_path=args.finding,
                review_path=args.review,
                actor=args.actor,
            )
            print(
                json.dumps(
                    {"status": "approved", "review_id": review.review_id, "connector_id": review.connector_id},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    except LabGateError as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
