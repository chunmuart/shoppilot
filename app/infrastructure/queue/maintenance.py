# -*- coding: utf-8 -*-
"""只裁剪已终结且所有消费组均已确认的消息；Redis 留永久幂等墓碑。"""
from __future__ import annotations

import json
from app.infrastructure.operational_metrics import observe_queue

from app.infrastructure.queue.redis_stream_queue import _STREAM, _LARGE_STREAM, _DEAD_STREAM, _STATUS_PREFIX, _lease_key

_PRUNE = """
local raw = redis.call('GET', KEYS[2])
if not raw or raw ~= ARGV[2] then return 0 end
local status = cjson.decode(raw)
if status.state ~= 'done' and status.state ~= 'failed' then return 0 end
if not status.terminal_at or tonumber(status.terminal_at) > tonumber(ARGV[4]) then return 0 end
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local function id_before(a,b)
  local am,as = string.match(a,'(%d+)%-(%d+)')
  local bm,bs = string.match(b,'(%d+)%-(%d+)')
  return tonumber(am) < tonumber(bm) or (tonumber(am) == tonumber(bm) and tonumber(as) < tonumber(bs))
end
local groups = redis.call('XINFO','GROUPS',KEYS[1])
if #groups == 0 then return 0 end
for _, group in ipairs(groups) do
  local name,last
  for i=1,#group,2 do
    if group[i] == 'name' then name=group[i+1] end
    if group[i] == 'last-delivered-id' then last=group[i+1] end
  end
  if not last or id_before(last,ARGV[1]) then return 0 end
  if #redis.call('XPENDING',KEYS[1],name,ARGV[1],ARGV[1],1) > 0 then return 0 end
end
if ARGV[5] ~= 'apply' then return 1 end
redis.call('SET',KEYS[2],ARGV[3])
return redis.call('XDEL',KEYS[1],ARGV[1])
"""

_PRUNE_DEAD = """
if redis.call('EXISTS',KEYS[4]) == 0 or redis.call('EXISTS',KEYS[2]) == 0 then return 0 end
if ARGV[3] ~= '' then
  local raw = redis.call('GET',KEYS[3])
  if not raw or raw ~= ARGV[3] then return 0 end
  local status = cjson.decode(raw)
  if status.state ~= 'failed' and status.state ~= 'done' then return 0 end
  if not status.terminal_at or tonumber(status.terminal_at) > tonumber(ARGV[4]) then return 0 end
else
  if tonumber(string.match(ARGV[1],'^(%d+)')) / 1000 > tonumber(ARGV[4]) then return 0 end
end
local function before(a,b)
  local am,as = string.match(a,'(%d+)%-(%d+)')
  local bm,bs = string.match(b,'(%d+)%-(%d+)')
  return tonumber(am) < tonumber(bm) or (tonumber(am) == tonumber(bm) and tonumber(as) < tonumber(bs))
end
local groups = redis.call('XINFO','GROUPS',KEYS[2])
if #groups == 0 then return 0 end
for _, group in ipairs(groups) do
  local name,last
  for i=1,#group,2 do
    if group[i] == 'name' then name=group[i+1] end
    if group[i] == 'last-delivered-id' then last=group[i+1] end
  end
  if not last or before(last,ARGV[2]) then return 0 end
  if #redis.call('XPENDING',KEYS[2],name,ARGV[2],ARGV[2],1) > 0 then return 0 end
end
local dead_groups = redis.call('XINFO','GROUPS',KEYS[1])
for _, group in ipairs(dead_groups) do
  local name,last
  for i=1,#group,2 do
    if group[i] == 'name' then name=group[i+1] end
    if group[i] == 'last-delivered-id' then last=group[i+1] end
  end
  if not last or before(last,ARGV[1]) then return 0 end
  if #redis.call('XPENDING',KEYS[1],name,ARGV[1],ARGV[1],1) > 0 then return 0 end
end
if ARGV[6] ~= 'apply' then return 1 end
if ARGV[3] ~= '' then redis.call('SET',KEYS[3],ARGV[5]) end
redis.call('XDEL',KEYS[2],ARGV[2])
local removed = redis.call('XDEL',KEYS[1],ARGV[1])
redis.call('DEL',KEYS[4])
return removed
"""


def _tombstone(status):
    compact = {key: status[key] for key in ("task_id", "state", "payload_fingerprint", "terminal_at", "stream", "message_id", "deliveries") if key in status}
    compact.update(archived=True, final_text="", error="")
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


async def maintain_queue(client, archive, *, older_than_seconds=7 * 86400, limit=1000, apply=False, cursors=None):
    if older_than_seconds < 0 or limit < 1:
        raise ValueError("保留期限不能为负，扫描上限须为正")
    # 终态时间与裁剪基准均来自 Redis，避免操作机时钟快导致提前删除。
    seconds, micros = await client.time()
    cutoff = seconds + micros / 1_000_000 - older_than_seconds
    cursors = dict(cursors or {})
    result = {"scanned": 0, "eligible": 0, "archived": 0, "deleted": 0, "skipped": 0, "apply": apply, "next_cursors": cursors}
    for stream in (_STREAM, _LARGE_STREAM):
        remaining = min(limit - result["scanned"], max(1, limit // 3))
        if remaining <= 0:
            break
        start = "-" if cursors.get(stream, "0-0") == "0-0" else f"({cursors[stream]}"
        entries = await client.xrange(stream, min=start, count=remaining)
        cursors[stream] = entries[-1][0] if len(entries) == remaining else "0-0"
        for message_id, fields in entries:
            result["scanned"] += 1
            try:
                payload = json.loads(fields.get("payload", ""))
                task_id = payload["task_id"]
                raw = await client.get(f"{_STATUS_PREFIX}{task_id}")
                status = json.loads(raw) if raw else {}
            except (ValueError, KeyError, TypeError):
                result["skipped"] += 1
                continue
            if status.get("state") not in {"done", "failed"} or not status.get("terminal_at"):
                result["skipped"] += 1
                continue
            tombstone = _tombstone(status)
            keys = [stream, f"{_STATUS_PREFIX}{task_id}"]
            eligible = await client.eval(_PRUNE, 2, *keys, message_id, raw, tombstone, cutoff, "check")
            if not eligible:
                result["skipped"] += 1
                continue
            result["eligible"] += 1
            if not apply:
                continue
            original_status = await archive.get_status(task_id) if status.get("archived") else status
            if original_status is None:
                raise RuntimeError("Redis 已是墓碑但完整归档不存在，拒绝继续清理")
            # 持久归档提交完成后，Lua 再次检查 pending/终态/版本，不能依赖预检查。
            await archive.put(stream, message_id, fields, original_status)
            result["archived"] += 1
            removed = int(await client.eval(_PRUNE, 2, *keys, message_id, raw, tombstone, cutoff, "apply"))
            result["deleted"] += removed
            if removed:
                observe_queue("archived")
                observe_queue("deleted")

    remaining = min(limit - result["scanned"], max(1, limit // 3))
    if remaining <= 0:
        return result
    start = "-" if cursors.get(_DEAD_STREAM, "0-0") == "0-0" else f"({cursors[_DEAD_STREAM]}"
    entries = await client.xrange(_DEAD_STREAM, min=start, count=remaining)
    cursors[_DEAD_STREAM] = entries[-1][0] if len(entries) == remaining else "0-0"
    for message_id, fields in entries:
        result["scanned"] += 1
        source, source_id, task_id = fields.get("stream"), fields.get("message_id"), fields.get("task_id", "")
        if source not in {_STREAM, _LARGE_STREAM} or not source_id:
            result["skipped"] += 1
            continue
        raw = await client.get(f"{_STATUS_PREFIX}{task_id}") if task_id else ""
        if task_id and not raw:
            result["skipped"] += 1
            continue
        status = json.loads(raw) if raw else {"task_id": "", "state": "failed"}
        tombstone = _tombstone(status)
        keys = [_DEAD_STREAM, source, f"{_STATUS_PREFIX}{task_id}", _lease_key("dead", f"{source}/{source_id}")]
        args = [message_id, source_id, raw, cutoff, tombstone]
        if not await client.eval(_PRUNE_DEAD, 4, *keys, *args, "check"):
            result["skipped"] += 1
            continue
        result["eligible"] += 1
        if apply:
            original_status = await archive.get_status(task_id) if status.get("archived") else status
            if original_status is None:
                raise RuntimeError("死信对应任务的完整归档不存在")
            # 坏 payload 也有可审计的原始来源；先保存源消息与 DLQ 两条记录。
            for _, original in await client.xrange(source, min=source_id, max=source_id):
                await archive.put(source, source_id, original, original_status)
            await archive.put(_DEAD_STREAM, message_id, fields, original_status)
            result["archived"] += 1
            removed = int(await client.eval(_PRUNE_DEAD, 4, *keys, *args, "apply"))
            result["deleted"] += removed
            if removed:
                observe_queue("archived")
                observe_queue("deleted")
    return result
