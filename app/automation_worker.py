"""独立调度进程：uv run python -m app.automation_worker。"""

import asyncio
import logging
import os
from app.composition import build_container
from app.application.automation import AutomationWorker


async def main():
    os.environ["AUTOMATION_WORKER_MODE"] = "external"
    from app.infrastructure.settings import load_settings

    if not load_settings().qdrant_url:
        raise RuntimeError(
            "独立 Worker 需要 QDRANT_URL 服务端索引；本地嵌入索引请使用默认 embedded 调度模式"
        )
    container = await build_container()
    await container.startup()
    try:
        await AutomationWorker(container.automation_service).run()
    finally:
        await container.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
