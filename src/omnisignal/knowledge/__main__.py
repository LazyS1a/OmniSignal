"""Explicit operator CLI: python -m omnisignal.knowledge publish --help."""
import argparse
import json
from pathlib import Path

from .release import KnowledgeError, _load, knowledge_root, load_private_key, make_record, publish_release, publish_withdrawals


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish already reviewed local knowledge; no crawling or model calls")
    parser.add_argument("action", choices=["publish", "withdraw"])
    parser.add_argument("--root", type=Path, default=knowledge_root())
    parser.add_argument("--input", type=Path, required=True, help="JSON config; cases are already reviewed")
    parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--private-key", type=Path, required=True, help="Ed25519 PEM, outside publish root")
    parser.add_argument("--key-id", required=True)
    args = parser.parse_args()
    try:
        config = _load(args.input, 32 * 1024 * 1024)
        key = load_private_key(args.private_key, args.root)
        common = dict(root=args.root, sequence=config["sequence"], private_key=key, key_id=args.key_id)
        if args.action == "withdraw":
            result = publish_withdrawals(**common, case_ids=config["case_ids"])
        else:
            if args.asset_root is None:
                parser.error("publish requires --asset-root")
            records = config.get("records")
            if records is None:
                records = [make_record(case) for case in config.get("cases", [])]
            result = publish_release(**common, release_id=config["release_id"], records=records, asset_root=args.asset_root, kind=config.get("kind", "full"), parent_release_id=config.get("parent_release_id"), taxonomy_version=config.get("taxonomy_version", "1"))
        print(json.dumps({"ok": True, "sequence": result["sequence"], "release_id": result.get("release_id")}, ensure_ascii=False))
    except (KnowledgeError, KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"Publication refused: {exc}\n")


if __name__ == "__main__":
    main()
