"""可信操作员本机签发短期买家令牌；密钥只从环境读取。"""
from __future__ import annotations

import argparse
import os

from app.infrastructure.identity import IdentityPolicy


def main() -> None:
    parser = argparse.ArgumentParser(description="本机签发 ShopPilot 短期买家身份令牌，无公开签发接口")
    parser.add_argument("--buyer-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args()
    policy = IdentityPolicy(mode="hmac", secret=os.getenv("IDENTITY_HMAC_SECRET", ""))
    print(policy.issue(args.buyer_id, ttl_seconds=args.ttl_seconds))


if __name__ == "__main__":
    main()
