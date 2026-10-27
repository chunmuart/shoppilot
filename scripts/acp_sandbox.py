"""ACP 沙箱商家及本机审批演示，不注册到生产 Agent 的交易工具。"""

import argparse
import asyncio
import json
from pathlib import Path
from app.infrastructure.commerce.acp import ACPClient, CheckoutApprovals


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="独立沙箱目录，禁止使用真实买家数据库",
    )
    p.add_argument("--port", type=int, default=8099)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--request-id", required=True)
    for name in ("approve", "reject", "reconcile"):
        parser = sub.add_parser(name)
        parser.add_argument("--id", required=True)
        if name != "reconcile":
            parser.add_argument("--snapshot-hash", required=True)
    args = p.parse_args()
    if args.command == "serve":
        import uvicorn
        from app.infrastructure.commerce.sandbox import create_sandbox_merchant

        uvicorn.run(
            create_sandbox_merchant(args.data_dir / "merchant.db"),
            host="127.0.0.1",
            port=args.port,
        )
        return

    async def run():
        service = CheckoutApprovals(
            args.data_dir / "approvals.db", ACPClient(f"http://127.0.0.1:{args.port}")
        )
        if args.command == "prepare":
            return await service.prepare(
                "sandbox-buyer",
                "sandbox-session",
                args.request_id,
                ["demo-backpack-black"],
            )
        if args.command == "reconcile":
            return await service.reconcile("sandbox-buyer", "sandbox-session", args.id)
        return await service.resolve(
            "sandbox-buyer",
            "sandbox-session",
            args.id,
            args.snapshot_hash,
            args.command == "approve",
        )

    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
