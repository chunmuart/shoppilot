"""可信操作员本机 Prompt 注册、成对门禁发布、回滚和紧急撤销；无网络发布接口。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.infrastructure.prompt_registry import PromptRegistry, toolset_contract


def main():
    parser = argparse.ArgumentParser(description="Prompt 不可变版本与人工发布")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    parser.add_argument("--web-search-enabled", action="store_true", default=bool(os.getenv("TAVILY_API_KEY", "")))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    importing = commands.add_parser("import")
    importing.add_argument("--yaml", type=Path, required=True)
    publishing = commands.add_parser("publish")
    for name in ("baseline", "candidate", "experiment"):
        publishing.add_argument("--" + name, required=True)
    publishing.add_argument("--candidate-bps", type=int, required=True)
    publishing.add_argument("--baseline-manifest", type=Path, required=True)
    publishing.add_argument("--candidate-manifest", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--deployment-id", required=True)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--version-id", required=True)
    revoke.add_argument("--reason", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    registry = PromptRegistry(args.data_dir / "prompts/registry.sqlite3", toolset_contract(root, web_search_enabled=args.web_search_enabled))
    try:
        if args.command == "import":
            result = registry._public_version(registry.import_version(args.yaml))
        elif args.command == "status":
            result = registry.describe()
        elif args.command == "publish":
            result = registry.publish(baseline=args.baseline, candidate=args.candidate, candidate_bps=args.candidate_bps,
                experiment=args.experiment, baseline_manifest=args.baseline_manifest, candidate_manifest=args.candidate_manifest)
        elif args.command == "rollback":
            result = {"deployment_id": registry.rollback(args.deployment_id), "scope": "new_sessions_only"}
        else:
            registry.revoke(args.version_id, args.reason)
            result = {"revoked": args.version_id, "scope": "existing_sessions_fail_closed"}
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
