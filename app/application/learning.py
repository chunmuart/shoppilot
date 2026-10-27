"""一次领取、独立反思、幂等落库。没有发布权限，也没有交易和训练工具。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import uuid

from app.domain.learning import Reflection


class LearningWorker:
    def __init__(self, store, extractor=None):
        self.store, self.extractor = store, extractor

    async def run_once(self):
        job = await asyncio.to_thread(self.store.claim, uuid.uuid4().hex)
        if job is None:
            return None

        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                await asyncio.to_thread(self.store.renew, job)

        heartbeat_task = asyncio.create_task(heartbeat())
        result = {"job_id": job["id"], "strategy_id": None, "sample_id": None, "usage": []}
        try:
            evidence = await asyncio.to_thread(self.store.evidence, job)
            outcome = evidence.get("outcome")
            if outcome == "failure":
                result["sample_id"] = await asyncio.to_thread(self.store.save_diagnostic, job, evidence)
                result["status"] = "diagnostic_saved"
            elif (not evidence.get("learning_allowed") or evidence.get("split") in {"holdout", "release"}
                  or not evidence.get("meaningful") or not evidence.get("facts") or evidence.get("pending_approval")):
                result["status"] = "skipped_insufficient_or_excluded_evidence"
            else:
                if not await asyncio.to_thread(self.store.partition_allows_learning, job, evidence):
                    result["status"] = "skipped_partition_conflict"
                    await asyncio.to_thread(self.store.finish, job, result=result)
                    return result
                if self.extractor is None:
                    raise ValueError("缺少已配置的独立提炼模型")
                reflected, usage = await asyncio.wait_for(self.extractor.extract(evidence), timeout=90)
                result["usage"] = usage
                reflected = Reflection.model_validate(reflected)
                result["assessment"] = reflected.outcome
                # 模型的成功判定只是筛选，不代表策略效果验证，更不触发发布。
                if reflected.outcome == "supported_success" and reflected.strategy:
                    result["strategy_id"] = await asyncio.to_thread(self.store.save_candidate, job, reflected.strategy.model_dump(), evidence, base=evidence.get("base"))
                    result["status"] = "candidate_captured"
                else:
                    result["status"] = "skipped_reviewer_not_supported"
            await asyncio.to_thread(self.store.finish, job, result=result)
            return result
        except asyncio.CancelledError as error:
            result.update(status="cancelled", usage=getattr(self.extractor, "last_usage", []))
            await asyncio.shield(asyncio.to_thread(self.store.finish, job, result=result, error=error))
            raise
        except Exception as error:
            result.update(status="retry_or_failed", error_type=type(error).__name__)
            # 提炼失败也记入逐次消耗，不能把失败模型调用算成零成本。
            result["usage"] = getattr(self.extractor, "last_usage", result["usage"])
            await asyncio.to_thread(self.store.finish, job, result=result, error=error)
            return result
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
