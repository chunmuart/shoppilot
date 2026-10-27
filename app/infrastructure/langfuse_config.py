"""Langfuse 的最小配置；不读取模型配置、不创建目录、不记录凭据。"""
from __future__ import annotations

import base64
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

LANGFUSE_FIELDS = ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True)
class LangfuseConfig:
    # 地址也不 repr，防止错误配置将密钥嵌在 URL 中。
    base_url: str = field(default="", repr=False)
    public_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env_file: str | Path | None = None,
                 environ: Mapping[str, str] | None = None) -> "LangfuseConfig":
        source = os.environ if environ is None else environ
        values = {}
        path = Path(env_file) if env_file is not None else (
            DEFAULT_ENV_FILE if environ is None and DEFAULT_ENV_FILE.is_file() else None)
        if path is not None:
            # 只使用项目默认文件或调用者指定文件；禁止 ${OTHER_SECRET} 隐式展开。
            try:
                with path.open(encoding="utf-8") as stream:
                    parsed = dotenv_values(stream=stream, interpolate=False)
            except (OSError, UnicodeError):
                raise ValueError("langfuse_env_file_unreadable") from None
            for name in LANGFUSE_FIELDS:
                if name in parsed:
                    values[name] = (parsed[name] or "").strip()
        if env_file is None:
            values.update({name: source[name].strip() for name in LANGFUSE_FIELDS if name in source})
        else:
            values = {**{name: source.get(name, "").strip() for name in LANGFUSE_FIELDS}, **values}
        return cls(*(values.get(name, "") for name in LANGFUSE_FIELDS))

    def missing_fields(self) -> list[str]:
        return [name for name, value in zip(LANGFUSE_FIELDS,
                (self.base_url, self.public_key, self.secret_key)) if not value]

    def validate(self) -> None:
        if self.missing_fields():
            raise ValueError("langfuse_configuration_incomplete")
        try:
            parsed = urlsplit(self.base_url)
            _ = parsed.port  # 触发标准库端口校验，异常信息不向外回显。
            is_local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            is_local = False
            try:
                parsed = urlsplit(self.base_url)
                _ = parsed.port
            except ValueError:
                raise ValueError("langfuse_base_url_invalid") from None
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and is_local))
                or any(char.isspace() for char in self.base_url)):
            raise ValueError("langfuse_base_url_invalid")
        if (":" in self.public_key or any(c in self.public_key + self.secret_key for c in "\r\n")
                or "${" in self.public_key + self.secret_key):
            raise ValueError("langfuse_credentials_invalid")

    @property
    def authorization_header(self) -> str:
        self.validate()
        value = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode("ascii")
        return "Basic " + value

    @property
    def otlp_traces_endpoint(self) -> str:
        self.validate()
        return self.base_url.rstrip("/") + "/api/public/otel/v1/traces"
