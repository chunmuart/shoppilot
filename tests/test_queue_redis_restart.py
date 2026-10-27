# -*- coding: utf-8 -*-
"""官方 Redis 进程 SIGKILL 后用原 AOF 恢复 PEL，再由真实 consumer reclaim。"""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest
import redis.asyncio as aioredis

from app.infrastructure.queue.redis_stream_queue import RedisStreamTaskQueue, _STREAM, _LARGE_STREAM, _GROUP, current_execution_lease
from tests.test_queue_reliability import task, eventually, consume, stop_consumer, has_state


async def test_sigkill_aof_restart_preserves_pending_and_reclaims_both_streams():
    binary = os.environ.get("SHOPPILOT_REDIS_SERVER_BIN") or shutil.which("redis-server")
    if not binary:
        pytest.skip("需要官方 redis-server 二进制进行真实进程重启验证")
    with tempfile.TemporaryDirectory(prefix="gbx-aof-", dir="/tmp") as directory:
        socket = Path(directory) / "redis.sock"
        arguments = [binary, "--port", "0", "--unixsocket", str(socket), "--unixsocketperm", "700",
                     "--dir", directory, "--save", "", "--appendonly", "yes", "--appendfsync", "always"]
        processes, clients = [], []
        with open(Path(directory) / "redis.log", "w+") as log:
            async def start():
                process = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT)
                processes.append(process)
                client = aioredis.Redis(unix_socket_path=str(socket), decode_responses=True, socket_timeout=.3)
                clients.append(client)
                async def ready():
                    try:
                        return await client.ping()
                    except Exception:
                        return False
                await eventually(ready)
                return process, client

            try:
                first, client = await start()
                queue = RedisStreamTaskQueue(client)
                await queue.ensure_group()
                ids = {}
                for index, stream in enumerate((_STREAM, _LARGE_STREAM)):
                    await queue.enqueue(task(f"aof-{index}", session=f"aof-s-{index}", priority=index))
                    entry = await client.xreadgroup(_GROUP, "crashed-worker", {stream: ">"}, count=1)
                    ids[stream] = entry[0][1][0][0]
                first.kill()  # SIGKILL：不走 SHUTDOWN，不给 Redis 优雅落盘机会。
                await asyncio.to_thread(first.wait, 5)
                await client.aclose()
                _, restored_client = await start()
                for stream in (_STREAM, _LARGE_STREAM):
                    pending = await restored_client.xpending_range(stream, _GROUP, "-", "+", 10)
                    assert pending[0]["message_id"] == ids[stream]
                    assert pending[0]["consumer"] == "crashed-worker"
                    assert pending[0]["times_delivered"] == 1

                restored_queue = RedisStreamTaskQueue(restored_client)
                received = []
                async def handler(_):
                    received.append(current_execution_lease().delivery)
                    return "AOF 重启后恢复完成"
                stop = asyncio.Event()
                runner = consume(restored_queue, handler, stop, concurrency=2)
                try:
                    async def complete():
                        return all([await has_state(restored_queue, f"aof-{i}", "done") for i in range(2)])
                    await eventually(complete)
                finally:
                    await stop_consumer(runner, stop)
                assert len(received) == 2
                assert {delivery.stream: delivery.message_id for delivery in received} == ids
                assert all(delivery.deliveries == 2 for delivery in received)
                for stream in (_STREAM, _LARGE_STREAM):
                    assert (await restored_client.xpending(stream, _GROUP))["pending"] == 0
            finally:
                for client in clients:
                    await client.aclose()
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            await asyncio.to_thread(process.wait, 4)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            await asyncio.to_thread(process.wait, 4)
