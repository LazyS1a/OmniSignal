"""Explicit local operator commands. No paid model call unless `work` is invoked."""
import argparse
import json
from pathlib import Path

from .preparation import PreparationStore, QwenAnalysisAdapter
from .release import KnowledgeError, _load, load_private_key


def main():
    parser = argparse.ArgumentParser(description="Prepare, review and publish signed knowledge")
    parser.add_argument("--root", type=Path, required=True, help="Editorial workspace, separate from publication directory")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "jobs", "audit", "work"):
        sub.add_parser(name)
    recover = sub.add_parser("recover")
    recover.add_argument("--actor", required=True)
    show = sub.add_parser("show")
    show.add_argument("case_id")
    imp = sub.add_parser("import")
    imp.add_argument("--input", type=Path, required=True)
    imp.add_argument("--asset-root", type=Path, required=True)
    imp.add_argument("--actor", required=True)
    for name in ("revise", "review", "enqueue"):
        command = sub.add_parser(name)
        command.add_argument("case_id")
        command.add_argument("--actor", required=True)
        command.add_argument("--revision", type=int, required=True)
        if name == "revise":
            command.add_argument("--input", type=Path, required=True)
        elif name == "review":
            command.add_argument("--decision", choices=["approve", "return"], required=True)
            command.add_argument("--note", default="")
        else:
            command.add_argument("--retry-failed", action="store_true")
    for name in ("publish", "withdraw"):
        command = sub.add_parser(name)
        command.add_argument("--publication-root", type=Path, required=True)
        command.add_argument("--input", type=Path, required=True, help="Publication sequence/release_id or withdrawal case_ids/reason")
        command.add_argument("--private-key", type=Path, required=True)
        command.add_argument("--key-id", required=True)
        command.add_argument("--actor", required=True)
    args = parser.parse_args()
    try:
        store = PreparationStore(args.root)
        if args.command == "list":
            result = store.list_cases()
        elif args.command == "show":
            result = store.get_case(args.case_id)
        elif args.command == "jobs":
            result = {"jobs": store.jobs(), "requests": store.tasks()}
        elif args.command == "audit":
            result = store.audit()
        elif args.command == "import":
            result = store.import_case(_load(args.input), args.asset_root, actor=args.actor)
        elif args.command == "revise":
            result = store.revise(args.case_id, _load(args.input), actor=args.actor, expected_revision=args.revision)
        elif args.command == "review":
            result = store.review(args.case_id, args.decision, actor=args.actor, expected_revision=args.revision, note=args.note)
        elif args.command == "enqueue":
            result = store.enqueue_analysis(args.case_id, actor=args.actor, expected_revision=args.revision, retry_failed=args.retry_failed)
        elif args.command == "work":
            result = store.run_pending(QwenAnalysisAdapter.from_env())
        elif args.command == "recover":
            result = {"interrupted_jobs": store.recover_interrupted(actor=args.actor)}
        else:
            config = _load(args.input)
            common = dict(publication_root=args.publication_root, sequence=config["sequence"],
                          private_key=load_private_key(args.private_key, args.publication_root), key_id=args.key_id, actor=args.actor)
            if args.command == "publish":
                result = store.publish(**common, release_id=config["release_id"], kind=config.get("kind", "full"),
                                       parent_release_id=config.get("parent_release_id"), case_ids=config.get("case_ids"))
            else:
                result = store.withdraw(**common, case_ids=config["case_ids"], reason=config["reason"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (KnowledgeError, KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"Preparation refused: {exc}\n")


if __name__ == "__main__":
    main()
