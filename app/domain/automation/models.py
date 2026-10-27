"""只计算计划，不执行任务；日历时间与 UTC 触发时刻明确分离。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["daily", "weekly", "once", "cron"] = "weekly"
    timezone: str = "Asia/Shanghai"
    time: str = "10:00"
    weekdays: list[int] = Field(default_factory=lambda: [1], max_length=7)
    cron: str = ""
    at: str | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_rule(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("请选择有效的 IANA 时区") from None
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.time):
            raise ValueError("时间格式应为 HH:mm")
        if self.kind == "weekly" and (
            not self.weekdays or any(d < 1 or d > 7 for d in self.weekdays)
        ):
            raise ValueError("请至少选择一个星期，周一为 1、周日为 7")
        if self.kind == "cron":
            if (
                len(self.cron.split()) != 5
                or len(self.cron) > 128
                or not re.fullmatch(r"[\d* /,\-]+", self.cron)
                or not croniter.is_valid(self.cron, strict=True)
            ):
                raise ValueError("仅支持五字段数字 cron：分 时 日 月 星期")
        if self.kind == "once":
            try:
                value = datetime.fromisoformat(self.at or "")
                if value.tzinfo is not None or self.local_instant(value) is None:
                    raise ValueError()
            except ValueError:
                raise ValueError("单次执行需提供任务时区内有效的本地日期时间") from None
        return self

    @property
    def expression(self):
        hour, minute = self.time.split(":")
        days = ",".join(str(d % 7) for d in sorted(set(self.weekdays)))
        return (
            self.cron
            if self.kind == "cron"
            else f"{int(minute)} {int(hour)} * * {days if self.kind == 'weekly' else '*'}"
        )

    def local_instant(self, value: datetime) -> float | None:
        # fold=0 只取重复本地时间的较早一次；UTC 往返检测不存在的时间。
        local = value.replace(tzinfo=ZoneInfo(self.timezone), fold=0)
        stamp = local.timestamp()
        restored = datetime.fromtimestamp(stamp, ZoneInfo(self.timezone)).replace(
            tzinfo=None
        )
        return stamp if restored == value else None

    def cutoff(self) -> float | None:
        if self.end_date is None:
            return None
        value = datetime.combine(self.end_date + timedelta(days=1), time.min)
        return value.replace(tzinfo=ZoneInfo(self.timezone)).timestamp()

    def next(self, after: float, *, reverse=False) -> float | None:
        cutoff = self.cutoff()
        if not reverse and cutoff is not None and after >= cutoff:
            return None
        if self.kind == "once":
            stamp = self.local_instant(datetime.fromisoformat(self.at))
            return (
                stamp
                if (stamp < after if reverse else stamp > after)
                and (cutoff is None or stamp < cutoff)
                else None
            )
        base = datetime.fromtimestamp(after, ZoneInfo(self.timezone)).replace(
            tzinfo=None
        )
        iterator = croniter(self.expression, base, max_years_between_matches=8)
        for _ in range(16):
            try:
                value = (
                    iterator.get_prev(datetime)
                    if reverse
                    else iterator.get_next(datetime)
                )
            except ValueError:
                return None
            stamp = self.local_instant(value)
            if stamp is None or (stamp >= after if reverse else stamp <= after):
                continue
            if cutoff is not None and stamp >= cutoff:
                if reverse:
                    return self.next(cutoff, reverse=True)
                return None
            return stamp
        return None

    def preview(self, now: float, count=5):
        result, cursor = [], now
        for _ in range(count):
            cursor = self.next(cursor)
            if cursor is None:
                break
            result.append(
                datetime.fromtimestamp(cursor, ZoneInfo(self.timezone)).isoformat()
            )
        return result


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=128)
    profile_revision: int = Field(ge=1)
    window_id: str
    thinking: str | None = None


class SkillBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str
    contentHash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AutomationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=12000)
    schedule: Schedule = Field(default_factory=Schedule)
    output_mode: Literal["independent", "merged"] = "merged"
    auto_start: bool = False
    model: ModelBinding
    skill: SkillBinding | None = None
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    locale: str = "zh-CN"

    @model_validator(mode="after")
    def trim(self):
        self.name, self.prompt = self.name.strip(), self.prompt.strip()
        if not self.name or not self.prompt:
            raise ValueError("名称与任务内容不能为空")
        return self
