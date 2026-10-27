"""Skill 600 条用例的数据合同、覆盖统计与真实存储流程适配器。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
import sqlite3

from app.domain.learning import digest, input_fingerprint
from scripts.eval.build_skill_cases import GROUPS, FAMILIES

DEFAULT_SUITE = Path(__file__).resolve().parents[2]/"eval/learning/skill-cases-v2.json"
SEARCH_FIELDS = {"ship_to", "target_currency", "price_max_major", "excluded_material_tags",
                 "required_material_tags", "sku_id", "product_id"}


def load_suite(path=DEFAULT_SUITE):
    suite = json.loads(Path(path).read_text())
    validate_suite(suite)
    return suite


def validate_suite(suite):
    from app.domain.catalog.money import SUPPORTED_CURRENCIES
    from app.domain.catalog.exchange_rate import ExchangeRateTable
    from app.domain.shipping.tariff_schedule import TariffSchedule
    destinations = TariffSchedule(ExchangeRateTable()).supported_destinations()
    if suite.get("schema_version") != "skill-eval-v2" or suite.get("repetitions") != 3:
        raise ValueError("未知用例合同或重复次数")
    cases = suite["cases"]
    if len(cases) != 600 or Counter(c["group"] for c in cases) != Counter({k: 100 for k in GROUPS}):
        raise ValueError("必须是六类各100条")
    ids, inputs, families = set(), set(), defaultdict(list)
    for case in cases:
        if case["id"] in ids or not re.fullmatch(r"[a-z0-9-]{1,80}", case["id"]) or case.get("synthetic") is not True:
            raise ValueError("ID 重复或不是合成用例")
        ids.add(case["id"])
        group, scenario = case["group"], case["scenario"]
        if scenario not in FAMILIES[group] or case["family"] != group+":"+scenario:
            raise ValueError("未知场景家族")
        if case["split"] not in {"dev", "holdout"}:
            raise ValueError("未知数据分区")
        families[case["family"]].append(case)
        if case["adapter"] == "model_search":
            if group in {"safety", "lifecycle"} or not case["query"].strip() or not case["reference"].strip():
                raise ValueError("模型用例缺少输入")
            if not case["expected"] or set(case["expected"]) - SEARCH_FIELDS:
                raise ValueError("断言包含当前工具不支持的字段")
            if case["expected"].get("ship_to") not in destinations or case["expected"].get("target_currency") not in SUPPORTED_CURRENCIES:
                raise ValueError("场景使用当前工具不支持的目的地或币种")
            for key, value in case["expected"].items():
                if key.endswith("_material_tags") and (not isinstance(value, list) or any(not isinstance(v, str) for v in value)):
                    raise ValueError("材质断言必须是字符串列表")
                if key == "price_max_major" and value is not None and (type(value) not in {int, float} or value <= 0):
                    raise ValueError("预算断言必须是正数或撤回后的null")
            if any(m["role"] not in {"user", "assistant"} or not m["content"] for m in case["history"]):
                raise ValueError("非法历史消息")
            fingerprint = input_fingerprint({"history": case["history"], "query": case["query"], "reference": case["reference"]})
        elif case["adapter"] == group and group in {"safety", "lifecycle"}:
            f = case["fixture"]
            if not 0 < f["quantity"] <= f["stock"] or f["price_minor"] <= 0 or f["currency"] not in SUPPORTED_CURRENCIES:
                raise ValueError("交易 fixture 不合法")
            fingerprint = input_fingerprint({"adapter": group, "scenario": scenario, "fixture": f})
        else:
            raise ValueError("未知适配器")
        if fingerprint in inputs:
            raise ValueError("输入重复，不允许靠换ID扩充")
        inputs.add(fingerprint)
    for name, rows in families.items():
        if len(rows) != 10 or {r["variant"] for r in rows} != set(range(1, 11)) or len({r["split"] for r in rows}) != 1:
            raise ValueError("同一家族须有10个变体且不能跨开发/留出集合")
    if len(families) != 60 or Counter(c["split"] for c in cases) != {"dev": 240, "holdout": 360}:
        raise ValueError("家族数或分区数量不正确")
    return {"cases": len(cases), "families": len(families), "dataset_sha256": digest(suite),
            "groups": {g: {"title": title, "total": 100, "dev": sum(c["group"] == g and c["split"] == "dev" for c in cases),
                            "holdout": sum(c["group"] == g and c["split"] == "holdout" for c in cases)} for g, title in GROUPS.items()},
            "adapters": dict(Counter(c["adapter"] for c in cases)), "duplicates": 0, "family_split_overlap": 0,
            "content_review": "规则生成并检查；不代表独立人工标注或模型效果验收"}


def select_cases(suite, profile):
    if profile == "all":
        return suite["cases"]
    if profile in {"dev", "release"}:
        split = "dev" if profile == "dev" else "holdout"
        return [c for c in suite["cases"] if c["split"] == split]
    if profile == "smoke":
        # 冒烟每类一个开发场景；不是600条发布验收。
        return [next(c for c in suite["cases"] if c["group"] == g and c["split"] == "dev") for g in GROUPS]
    raise ValueError("未知执行档位")


def skill_document(case):
    return {"kind": "skill", "id": "fixture-skill", "version": "1", "title": "选购步骤参考",
            "description": "选购约束与查询步骤；以买家当前明确要求为准", "body": case.get("reference", case.get("fixture", {}).get("body")),
            "scope": "shopping", "allowed_tools": ["product_search_tool"], "keywords": [], "expires_at": None,
            "evidence": [{"reference": "synthetic:"+case["id"], "summary": "隔离评测的人工构造参考，非已验证生产策略"}]}


def publish_fixture(registry, doc):
    registry.import_draft(doc, author="synthetic-eval")
    registry.review(doc["kind"], doc["id"], doc["version"], reviewer="synthetic-eval", evidence="仅在隔离库构造测试状态")
    return registry.publish(doc["kind"], doc["id"], doc["version"], publisher="synthetic-eval", evidence="测试fixture不代表发布验收")


def lifecycle_observation(case, folder):
    from app.infrastructure.buyer_skills import BuyerSkillStore
    from app.infrastructure.capability_registry import CapabilityRegistry
    f, scenario = case["fixture"], case["scenario"]
    path = folder/"skills.db"
    store = BuyerSkillStore(path)
    original = store.save(f["buyer_id"], f["title"], "合成方案", f["body"])
    args = (f["buyer_id"], original["id"], original["version"])
    try:
        if scenario == "same_save":
            result = store.save(f["buyer_id"], f["title"], "合成方案", f["body"], skill_id=original["id"], expected_version=original["version"])
            return {"result": "same_version" if result["version"] == original["version"] else "changed"}
        if scenario == "stale_save":
            store.save(f["buyer_id"], f["title"], "新用途", f["body"]+"修订", skill_id=original["id"], expected_version=original["version"])
            store.save(f["buyer_id"], f["title"], "过时修改", f["body"], skill_id=original["id"], expected_version=original["version"])
        elif scenario == "reopen":
            loaded = BuyerSkillStore(path).load(*args)
            return {"result": "same_content" if loaded["content_hash"] == original["content_hash"] and loaded["body"] == f["body"] else "changed"}
        elif scenario == "delete":
            store.delete(*args); store.load(*args)
        elif scenario == "wrong_owner":
            store.load("different-buyer", *args[1:])
        elif scenario == "tamper":
            with sqlite3.connect(path) as db:
                db.execute("UPDATE buyer_skill_versions SET payload='{}'")
            store.load(*args)
        else:
            registry = CapabilityRegistry(folder/"registry.db")
            doc = skill_document(case)
            if scenario == "tool_escalation":
                registry.import_draft({**doc, "allowed_tools": ["execute_shell"]}, author="synthetic-eval")
            elif scenario == "unreviewed":
                registry.import_draft(doc, author="synthetic-eval")
                registry.publish("skill", doc["id"], "1", publisher="synthetic-eval", evidence="未经审核")
            else:
                publish_fixture(registry, doc)
                if scenario == "revoke":
                    registry.revoke("skill", doc["id"], "1", actor="synthetic-eval", reason="测试撤回")
                    registry.load_skill(doc["id"], "1", available_tools={"product_search_tool"})
                elif scenario == "session_changed":
                    registry.bind_session(f["session_id"], f["buyer_id"])
                    publish_fixture(registry, {**doc, "version": "2", "body": doc["body"]+"新版本"})
                    registry.bind_session(f["session_id"], f["buyer_id"])
                else:
                    raise RuntimeError("未实现的生命周期适配器")
    except (ValueError, LookupError) as error:
        return {"result": type(error).__name__}
    return {"result": "unexpected_accept"}


async def safety_observation(case, folder):
    from sqlalchemy import func, select, update
    from app.domain.catalog.money import Money
    from app.domain.catalog.product import Product
    from app.domain.catalog.sku import Sku
    from app.domain.order.ports.trade_store import TradeStoreError
    from app.infrastructure.persistence.sql.repositories import create_engine
    from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
    from app.infrastructure.persistence.sql.tables import OrderRow
    from app.infrastructure.persistence.sql.trade_tables import SkuInventoryRow
    f, scenario = case["fixture"], case["scenario"]
    stamp = datetime(2026, 10, 3, tzinfo=timezone.utc)
    engine = create_engine("sqlite+aiosqlite:///"+str(folder/"trade.db"))
    store = SqlTradeStore(engine, clock=lambda: stamp)
    def product(stock=f["stock"], price=f["price_minor"], currency=f["currency"]):
        return Product(product_id="fixture-product", title=f["title"], brand="fixture", category="fixture", origin_country="CN", description="合成商品",
                       skus=[Sku("fixture-sku", "标准", Money.of(price, currency), stock)])
    try:
        await store.initialize_inventory([product()])
        payload = {"items": [{"product_id": "fixture-product", "sku_id": "fixture-sku", "title": f["title"],
                              "unit_price_minor": f["price_minor"], "currency": f["currency"], "quantity": f["quantity"]}],
                   "shipping_address": {"recipient_name": "合成买家", "country": "CN", "state": "测试省", "city": "测试城",
                                        "address_line": "测试地址", "postal_code": "000000", "phone": ""}}
        confirmation = await store.prepare_confirmation(operation_id="fixture-operation", buyer_id=f["buyer_id"], session_id=f["session_id"],
                                                       action="create", payload=payload, expires_at=stamp+timedelta(minutes=5))
        kwargs = {"buyer_id": f["buyer_id"], "session_id": f["session_id"], "snapshot_hash": confirmation["snapshot_hash"], "approved": True}
        if scenario == "wrong_buyer": kwargs["buyer_id"] = "not-the-owner"
        if scenario == "wrong_session": kwargs["session_id"] = "another-session"
        if scenario == "bad_hash": kwargs["snapshot_hash"] = "0"*64
        if scenario == "expired": store = SqlTradeStore(engine, clock=lambda: stamp+timedelta(hours=1))
        if scenario == "stock_changed":
            # 模拟另一买家耗尽库存；目录初始化按设计不会重置持久库存。
            async with engine.begin() as db:
                await db.execute(update(SkuInventoryRow).values(stock=0))
        if scenario == "price_changed": await store.initialize_inventory([product(price=f["price_minor"]+1)])
        if scenario == "currency_changed": await store.initialize_inventory([product(currency="USD" if f["currency"] != "USD" else "CNY")])
        observation = {}
        try:
            result = confirmation
            if scenario != "pending":
                result = await store.resolve_confirmation(confirmation["confirmation_id"], **{**kwargs, "approved": scenario != "reject"})
            if scenario == "replay":
                again = await store.resolve_confirmation(confirmation["confirmation_id"], **kwargs)
                if result != again: observation["replay_mismatch"] = True
            observation["status"] = result["status"]
        except TradeStoreError as error:
            observation["error"] = error.code
        inventory = await store.get_inventory()
        observation["stock_delta"] = inventory["fixture-sku"] - f["stock"]
        current = await store.get_confirmation(confirmation["confirmation_id"], buyer_id=f["buyer_id"], session_id=f["session_id"])
        observation["confirmation_status"] = current["status"]
        async with engine.connect() as db:
            observation["orders"] = await db.scalar(select(func.count()).select_from(OrderRow))
        return observation
    finally:
        await engine.dispose()


async def run_contract(case, folder):
    folder.mkdir(parents=True, exist_ok=False)
    if case["adapter"] == "lifecycle":
        actual = lifecycle_observation(case, folder)
    elif case["adapter"] == "safety":
        actual = await safety_observation(case, folder)
    else:
        raise ValueError("模型场景不能当作确定性流程运行")
    failed = [key for key, value in case["expected"].items() if actual.get(key) != value]
    if actual.get("replay_mismatch"): failed.append("replay_mismatch")
    return {"case_id": case["id"], "group": case["group"], "family": case["family"], "split": case["split"],
            "execution": "production_sqlite_contract", "passed": not failed, "violations": failed, "actual": actual}
