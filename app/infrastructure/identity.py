"""可选本机签名身份：固定 HS256、用途、签发方、受众与有效期。无公开签发接口。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import jwt


class IdentityError(ValueError):
    pass


def buyer_identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(character) < 32 for character in value):
        raise IdentityError("买家身份必须为有效的非空标识")
    return value.strip()


@dataclass(frozen=True)
class IdentityPolicy:
    mode: str = "demo"
    secret: str = field(default="", repr=False)
    issuer: str = "shoppilot-local"
    audience: str = "shoppilot-api"
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.mode not in {"demo", "hmac"}:
            raise ValueError("IDENTITY_MODE 仅支持 demo 或 hmac")
        if self.mode == "hmac" and len(self.secret.encode("utf-8")) < 32:
            raise ValueError("hmac 身份模式需要至少 32 字节的 IDENTITY_HMAC_SECRET")

    @classmethod
    def from_settings(cls, settings):
        policy = cls(mode=settings.identity_mode, secret=settings.identity_hmac_secret)
        if policy.mode == "hmac" and not settings.session_owner_binding:
            raise ValueError("hmac 严格模式必须启用 SESSION_OWNER_BINDING")
        return policy

    def issue(self, buyer_id: str, *, ttl_seconds: int = 3600) -> str:
        if self.mode != "hmac":
            raise ValueError("只有配置了服务端密钥的本机签发程序可以创建令牌")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("令牌有效期必须为 1 至 86400 秒")
        now = int(self.clock())
        return jwt.encode({"sub": buyer_identity(buyer_id), "iat": now, "exp": now + ttl_seconds,
            "iss": self.issuer, "aud": self.audience}, self.secret, algorithm="HS256",
            headers={"typ": "shoppilot-access+jwt"})

    def verify(self, token: str) -> str:
        if self.mode != "hmac" or not isinstance(token, str) or len(token) > 8192:
            raise IdentityError("身份凭证无效或已过期")
        try:
            header = jwt.get_unverified_header(token)
            if header != {"alg": "HS256", "typ": "shoppilot-access+jwt"}:
                raise IdentityError("身份凭证类型无效")
            payload = jwt.decode(token, self.secret, algorithms=["HS256"], issuer=self.issuer, audience=self.audience,
                options={"require": ["sub", "iat", "exp", "iss", "aud"], "verify_exp": False, "verify_iat": False,
                         "strict_aud": True})
            if set(payload) != {"sub", "iat", "exp", "iss", "aud"}:
                raise IdentityError("身份凭证内容无效")
            now = self.clock()
            if (type(payload["iat"]) is not int or type(payload["exp"]) is not int
                    or payload["iat"] > now + 30 or payload["exp"] <= now
                    or not 1 <= payload["exp"] - payload["iat"] <= 86400):
                raise IdentityError("身份凭证无效或已过期")
            return buyer_identity(payload["sub"])
        except (jwt.InvalidTokenError, IdentityError, TypeError, ValueError) as error:
            raise IdentityError("身份凭证无效或已过期") from error
