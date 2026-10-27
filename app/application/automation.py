"""调度业务与执行适配。定时、手动运行使用同一持久入口。"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import uuid
from zoneinfo import ZoneInfo
from ag_ui.core import RunAgentInput
from app.domain.automation.models import AutomationDefinition
from app.application.agents.orchestrator import SubmitIntentInput
from app.application.agents.selected_skill import SelectedSkill
from app.infrastructure.automation_store import AutomationConflict
from app.infrastructure.ag_ui_journal import JournalConflict

logger = logging.getLogger(__name__)
TEMPLATES = [
    {
        "id": "weekly-picks",
        "name": "每周通勤好物",
        "description": "结合预算与长期偏好，定期整理一份有依据的选购清单。",
        "prompt": "帮我挑选适合日常通勤的轻便双肩包，单件预算 500 元人民币以内，收货地中国。查询并比较最多 8 件候选，说明材质、规格、到手价与选择理由。遵守本会话中我后续补充的最新要求。",
        "time": "10:00",
        "weekdays": [1],
    },
    {
        "id": "compare",
        "name": "接着上次继续比较",
        "description": "保留已选候选与排除条件，重新核实仍需确认的商品信息。",
        "prompt": "回顾本会话中我明确选择的商品与最新要求，重新查询候选的当前价格和库存，简要更新比较结果。没有已选候选时先根据我的偏好给出候选；不代我下单。",
        "time": "18:00",
        "weekdays": [5],
    },
    {
        "id": "review",
        "name": "选购进展复盘",
        "description": "整理已经确定的需求、待选商品与尚未解决的问题。",
        "prompt": "整理本会话的选购进展：我已经确定的需求、保留和排除的候选、仍需确认的信息。区分历史观察与当前事实，不把未核验价格当成最新报价。",
        "time": "20:00",
        "weekdays": [7],
    },
]


class AutomationService:
    def __init__(self, store, runtime, registry, session_store):
        self.store, self.runtime, self.registry, self.sessions = (
            store,
            runtime,
            registry,
            session_store,
        )

    async def validate(self, buyer, definition):
        value = AutomationDefinition.model_validate(definition)
        if self.registry is None:
            raise AutomationConflict("模型目录尚未配置")
        binding = value.model
        # 检查当前版本，避免历史 profile 仍可解析时静默沿用过期授权。
        snapshot = await self.registry.resolve(
            binding.profile_id, binding.window_id, binding.thinking, buyer_id=buyer
        )
        if snapshot["profile_revision"] != binding.profile_revision:
            raise AutomationConflict("模型配置已更新，请重新选择并确认")
        if value.skill:
            catalog = await self.runtime.orchestrator.available_skills(buyer)
            items = catalog.get("skills", []) if isinstance(catalog, dict) else catalog
            if not any(
                s["id"] == value.skill.id
                and str(s["version"]) == value.skill.version
                and s["content_hash"] == value.skill.contentHash
                for s in items
            ):
                raise AutomationConflict("Skill 已更新或不可用，请重新选择")
        return value, snapshot

    async def save(self, buyer, definition, request_id, **kwargs):
        value, _ = await self.validate(buyer, definition)
        return await self.store.save(
            buyer, value.model_dump(mode="json"), request_id, **kwargs
        )

    async def execute(self, claim):
        row, attempt = claim["run"], claim["attempt"]
        try:
            value, snapshot = await self.validate(row["buyer_id"], row["definition"])
            if self.runtime.confirmations:
                confirmations = await self.runtime.confirmations.list(
                    row["buyer_id"], row["session_id"]
                )
                if any(
                    x["status"] == "pending" and not x.get("expired")
                    for x in confirmations["confirmations"]
                ):
                    raise JournalConflict("当前会话有待确认的交易，请先处理")
            await self.sessions.assert_owner(
                row["session_id"], row["buyer_id"], create=True, enforce_owner=True
            )
            await self.store.prepared(attempt, snapshot)
            stamp = datetime.fromtimestamp(
                row["scheduled_for"], ZoneInfo(value.schedule.timezone)
            ).strftime("%Y-%m-%d %H:%M %Z")
            # 仅追加本轮输入；历史由同 session 的 AgentState.context 恢复。
            content = (
                f"自动执行 · {value.name}\n计划时间：{stamp}\n"
                f"任务设置版本：{row['definition_revision']}\n"
                "以下为买家设置的周期任务默认要求；同一会话中买家后续明确修改的预算、选择和限制优先。"
                "请结合已有历史完成本次任务，重新核验需要最新状态的价格和库存。"
                "写操作仍需买家确认，不得自动批准。\n\n" + value.prompt
            )
            props = {
                "buyerId": row["buyer_id"],
                "locale": value.locale,
                "currency": value.currency,
            }
            if value.skill:
                props["selectedSkill"] = value.skill.model_dump()
            body = RunAgentInput.model_validate(
                {
                    "threadId": row["session_id"],
                    "runId": attempt["agent_run_id"],
                    "messages": [
                        {
                            "id": "trigger-" + attempt["id"],
                            "role": "user",
                            "content": content,
                        }
                    ],
                    "state": {},
                    "tools": [],
                    "context": [],
                    "forwardedProps": props,
                }
            )
            intent = SubmitIntentInput(
                row["session_id"],
                row["buyer_id"],
                value.locale,
                value.currency,
                content,
                selected_skill=SelectedSkill.parse(props["selectedSkill"])
                if value.skill
                else None,
                model_override=snapshot,
            )
            await self.runtime.start(body, intent, automation_ticket=attempt)
        except asyncio.CancelledError:
            raise
        except JournalConflict as error:
            await self.store.fail_start(claim, str(error), "skipped_overlap")
        except Exception as error:
            logger.warning("自动化启动失败 %s: %s", row["id"], type(error).__name__)
            # 未知异常不把网关、密钥或买家原文写入失败记录。
            message = (
                str(error)
                if isinstance(error, AutomationConflict)
                else "配置校验或启动失败，请检查模型、Skill 与服务状态"
            )
            await self.store.fail_start(claim, message)


class AutomationWorker:
    def __init__(self, service, *, interval=2):
        self.service, self.interval = service, interval
        self.id = "worker-" + uuid.uuid4().hex

    async def tick(self):
        service = self.service
        await service.runtime.journal.recover_expired()
        await service.store.reconcile(service.runtime.confirmations)
        await service.store.tick(self.id)
        for _ in range(2):
            claim = await service.store.claim(self.id)
            if claim is None:
                break
            await service.execute(claim)

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("自动化调度循环失败，下轮将从数据库恢复")
            await asyncio.sleep(self.interval)
