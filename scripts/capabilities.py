# -*- coding: utf-8 -*-
"""本机能力审核 CLI。Agent 无此写入口；操作员身份是本机声明，依赖主机访问控制。"""
import argparse
import json
from pathlib import Path

from app.infrastructure.capability_registry import CapabilityRegistry
from app.infrastructure.settings import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="隔离审核库路径；默认 DATA_DIR/capabilities.db")
    commands = parser.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import")
    importing.add_argument("path", type=Path)
    importing.add_argument("--author", required=True)
    for name, actor, proof in (("review", "reviewer", "evidence"), ("publish", "publisher", "evidence"), ("revoke", "actor", "reason")):
        command = commands.add_parser(name)
        command.add_argument("kind", choices=["skill", "strategy"])
        command.add_argument("id")
        command.add_argument("version")
        command.add_argument(f"--{actor}", required=True)
        command.add_argument(f"--{proof}", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--kind", choices=["skill", "strategy"])
    commands.add_parser("audit")
    showing = commands.add_parser("show")
    showing.add_argument("kind", choices=["skill", "strategy"])
    showing.add_argument("id")
    showing.add_argument("version")
    args = parser.parse_args(argv)
    registry = CapabilityRegistry(args.db or load_settings().data_dir / "capabilities.db")
    if args.command == "import":
        output = registry.import_draft(json.loads(args.path.read_text(encoding="utf-8")), author=args.author)
    elif args.command == "review":
        output = registry.review(args.kind, args.id, args.version, reviewer=args.reviewer, evidence=args.evidence)
    elif args.command == "publish":
        output = registry.publish(args.kind, args.id, args.version, publisher=args.publisher, evidence=args.evidence)
    elif args.command == "revoke":
        output = registry.revoke(args.kind, args.id, args.version, actor=args.actor, reason=args.reason)
    elif args.command == "list":
        output = registry.inspect(kind=args.kind)
    elif args.command == "show":
        output = registry.show(args.kind, args.id, args.version)
    else:
        output = registry.audit()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
