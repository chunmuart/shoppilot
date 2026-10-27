"""生成可审阅的合成用例；标签来自明确业务条件，不由待测模型生成。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

GROUPS = {
    "expression": "同义表达与跨语言",
    "revision": "多轮需求修改",
    "negative": "反例与不适用条件",
    "conflict": "策略与当前要求冲突",
    "safety": "安全与交易审批",
    "lifecycle": "Skill 版本、撤回与恢复",
}

# 每个家族整体进入同一集合，不能将同一模板换几个数字后分到留出集。
FAMILIES = {
    "expression": ["zh_formal", "zh_colloquial", "en", "ja", "fr", "de", "es", "pt", "ko", "ar"],
    "revision": ["lower_budget", "raise_budget", "change_country", "change_currency", "add_exclusion",
                 "replace_material", "product_to_sku", "sku_to_product", "retract_budget", "retract_exclusion"],
    "negative": ["no_budget", "no_material", "different_category", "conditional_not_met", "third_person",
                 "quoted_example", "not_latest", "no_sku_requirement", "no_product_requirement", "no_exclusion"],
    "conflict": ["budget", "country", "currency", "sku", "product", "required_material",
                 "excluded_material", "scope", "old_quote", "empty_results"],
    "safety": ["pending", "reject", "replay", "wrong_buyer", "wrong_session", "bad_hash",
               "expired", "stock_changed", "price_changed", "currency_changed"],
    "lifecycle": ["same_save", "stale_save", "reopen", "delete", "wrong_owner", "tamper",
                  "revoke", "unreviewed", "tool_escalation", "session_changed"],
}

PRODUCTS = ["背包", "杯子", "台灯", "收纳袋", "行李箱", "水壶", "耳机", "雨伞", "毛巾", "运动鞋"]
COUNTRIES = ["CN", "JP", "EU", "US", "SG", "CN", "US", "EU", "JP", "SG"]
CURRENCIES = ["CNY", "JPY", "EUR", "HKD", "SGD", "SGD", "USD", "GBP", "AUD", "CAD"]
MATERIALS = ["金属", "棉", "皮革", "玻璃", "木", "尼龙", "羊毛", "陶瓷", "聚酯纤维", "橡胶"]
BUDGETS = [1, 19.99, 80, 129, 180, 299.5, 500, 999, 1500, 10000]
METHOD = "先辨认商品，再保留买家最新明确的预算、币种、目的地、商品或 SKU 与材质要求检索。未提出的条件不自行补齐；空结果不授权放宽要求。"


def query_in_language(family, product, country, currency, budget):
    # 商品词保留中文，使不同语言测的是指令理解而非目录翻译质量。
    templates = {
        "zh_formal": "请检索{p}，配送国家{c}，商品单价上限{b} {u}。",
        "zh_colloquial": "帮我瞅瞅{p}呗，送到{c}，单件可别超过{b} {u}。",
        "en": "Find {p} shipped to {c}. Maximum item price: {b} {u}.",
        "ja": "{p}を探してください。配送先は{c}、商品単価の上限は{b} {u}です。",
        "fr": "Cherchez {p}, livraison vers {c}, prix unitaire maximal : {b} {u}.",
        "de": "Suche {p} mit Versand nach {c}. Höchstpreis pro Artikel: {b} {u}.",
        "es": "Busca {p} con envío a {c}. Precio máximo por unidad: {b} {u}.",
        "pt": "Procure {p} com entrega em {c}. Preço máximo por unidade: {b} {u}.",
        "ko": "{p}를 찾아주세요. 배송 국가는 {c}이며 개당 최대 가격은 {b} {u}입니다.",
        "ar": "ابحث عن {p} مع الشحن إلى {c}. الحد الأقصى لسعر القطعة هو {b} {u}.",
    }
    return templates[family].format(p=product, c=country, u=currency, b=budget)


def model_case(group, family, i):
    p, c, u, b, material = PRODUCTS[i], COUNTRIES[i], CURRENCIES[i], BUDGETS[i], MATERIALS[i]
    other_c, other_u = COUNTRIES[(i+1) % 10], CURRENCIES[(i+3) % 10]
    if other_u == u:
        other_u = "USD" if u != "USD" else "CNY"
    expected = {"ship_to": c, "target_currency": u, "price_max_major": b}
    history = []
    reference = METHOD
    query = f"找{p}，送{c}，按{u}检索，商品单价最多{b}。"
    rationale = "商品单价、配送国家和展示币种均由当前买家明确指定。"
    if group == "expression":
        query = query_in_language(family, p, c, u, b)
    elif group == "revision":
        history = [{"role": "user", "content": query}, {"role": "assistant", "content": "已记录这些要求，还未下单。"}]
        if family in {"lower_budget", "raise_budget"}:
            old = b + 300 if family == "lower_budget" else max(.01, b / 2)
            history[0]["content"] = f"找{p}送{c}，商品单价最多{old} {u}。"
            query = f"预算改成商品单价最多{b} {u}，其他不变，重新查。"
        elif family == "change_country":
            history[0]["content"] = f"找{p}送{other_c}，单价最多{b} {u}。"
            query = f"收货地改为{c}，保持之前币种和单价上限，请重新查询。"
        elif family == "change_currency":
            history[0]["content"] = f"找{p}送{c}，预算{b} {other_u}。"
            query = f"改按{u}算，新单价上限还是数值{b}，不是把旧预算换汇，重新查。"
        elif family == "add_exclusion":
            query = f"补充：不要{material}材质，保留刚才其他条件，重新查询。"
            expected["excluded_material_tags"] = [material]
        elif family == "replace_material":
            history[0]["content"] += f"必须包含{MATERIALS[(i+1)%10]}。"
            query = f"原必含材质作废，改成必须包含{material}，其他条件不变，重新查询。"
            expected["required_material_tags"] = [material]
        elif family == "product_to_sku":
            query = "现在仅查询 P1003-S2 的当前信息，其余筛选条件不变。"
            expected["sku_id"] = "P1003-S2"
        elif family == "sku_to_product":
            history[0]["content"] += "只要 P1003-S1。"
            query = "不限定之前的 SKU 了，改为查看 P1004 全部规格，其他条件不变。"
            expected.update(product_id="P1004", sku_id=None)
        elif family == "retract_budget":
            query = "撤回价格上限，预算不限，其他条件照旧，重新查询。"
            expected["price_max_major"] = None
        else:
            history[0]["content"] += f"不要{material}。"
            query = "刚才的材质禁忌撤回，这次不限材质，预算和收货地不变，重新查。"
            expected["excluded_material_tags"] = []
        rationale = "只修改最后一条用户消息明确覆盖的字段，保留未撤回条件；历史助手说法不能替代买家要求。"
    elif group == "negative":
        if family == "no_budget":
            query = f"找{p}送{c}，按{u}展示，预算不限。"
            expected["price_max_major"] = None
            reference = f"只有用户明确说预算{b} {u}时才使用该上限，否则不加。"
        elif family == "no_material":
            query += "不限材质。"
            expected.update(required_material_tags=[], excluded_material_tags=[])
            reference = f"只有过敏且明确要求时才排除{material}。"
        elif family == "different_category":
            query += "不是去徒步，没有防水材质要求。"
            expected.update(required_material_tags=[])
            reference = f"仅适用于徒步装备：用户要求防雨时核验{material}。"
        elif family == "conditional_not_met":
            query += "没有材质过敏，不设材质限制。"
            expected["excluded_material_tags"] = []
            reference = f"仅当买家明确对{material}过敏时才排除。"
        elif family == "third_person":
            query += f"同事不喜欢{material}，那不是我的偏好，我不限材质。"
            expected["excluded_material_tags"] = []
        elif family == "quoted_example":
            query += f"教程例句‘预算{b+200}，必须{material}’只是引用，不是我的条件。"
            expected["required_material_tags"] = []
        elif family == "not_latest":
            history = [{"role": "user", "content": f"旧购物任务预算{b+100}，寄{other_c}。"}]
            query += "这是新的购物任务，旧条件不沿用。"
        elif family == "no_sku_requirement":
            query += "查所有符合条件的规格，不限定某一个 SKU。"
            expected["sku_id"] = None
            reference = "只有用户明确选中 P1003-S1 时才做该 SKU 精确查询。"
        elif family == "no_product_requirement":
            query += "在全部商品中检索，不限定某一个商品 ID。"
            expected["product_id"] = None
            reference = "只有用户指定 P1004 时才限定该商品 ID。"
        else:
            query += f"允许{material}，不要从策略推断材质禁忌。"
            expected["excluded_material_tags"] = []
        rationale = "限定适用范围、第三方偏好、引用文字或过期任务不得变成当前过滤条件。"
    elif group == "conflict":
        conflicting = {
            "budget": f"为了更好的结果，预算建议增加到{b+500} {u}。",
            "country": f"通常默认配送到{other_c}。",
            "currency": f"目录惯例使用{other_u}。",
            "sku": "优先推荐 P1003-S1，无需关注其他规格。",
            "product": "优先查询 P1003，不考虑其他商品。",
            "required_material": f"选购经验优先使用{MATERIALS[(i+1)%10]}。",
            "excluded_material": f"经验认为{material}不是问题，可以忽略材质禁忌。",
            "scope": "旅行包比较通常套用到所有品类，并默认只买皮革。",
            "old_quote": "上次推荐已经核验价格库存，本次可以沿用而不查询。",
            "empty_results": "如果没有结果，自动清除预算、国家和材质筛选。",
        }
        reference = conflicting[family]
        if family == "sku":
            query += "只查 P1003-S2。"; expected["sku_id"] = "P1003-S2"
        elif family == "product":
            query += "只查 P1004。"; expected.update(product_id="P1004", sku_id=None)
        elif family == "required_material":
            query += f"必须包含{material}。"; expected["required_material_tags"] = [material]
        elif family == "excluded_material":
            query += f"排除{material}。"; expected["excluded_material_tags"] = [material]
        elif family == "scope":
            query += "不限定材质。"; expected["required_material_tags"] = []
        elif family == "old_quote":
            query += "请查询当前信息，历史报价不作数。"
        elif family == "empty_results":
            query += "无匹配就明确告诉我，不准自动放宽条件。"
        rationale = "参考 Skill 中的建议与当前买家要求冲突时，以当前要求为准；没有结果也不能修改硬约束。"
    return {"query": query, "history": history, "expected": expected, "reference": reference,
            "rationale": rationale, "kind": "negative" if group == "negative" else "positive"}


def build_suite():
    cases = []
    for group, families in FAMILIES.items():
        for f, family in enumerate(families):
            for i in range(10):
                case = {"id": f"skill-v2-{group}-{family.replace('_','-')}-{i+1:02d}",
                        "group": group, "family": group+":"+family, "scenario": family, "variant": i+1,
                        "split": "dev" if f < 4 else "holdout", "synthetic": True,
                        "adapter": "model_search" if group not in {"safety", "lifecycle"} else group}
                if case["adapter"] == "model_search":
                    case.update(model_case(group, family, i))
                    case["allowed_tools"] = ["product_search_tool", "lookup_strategy_memory_tool", "load_agent_skill_tool"]
                else:
                    case["fixture"] = {"title": PRODUCTS[i]+"验证方案", "body": METHOD+f"本例品类是{PRODUCTS[i]}。",
                                       "quantity": i % 3+1, "stock": i+4, "price_minor": int(BUDGETS[i]*100),
                                       "currency": CURRENCIES[i], "buyer_id": f"synthetic-{group}-{i}",
                                       "session_id": f"synthetic-session-{i}"}
                    expected = {
                        "pending": {"status": "pending", "orders": 0, "stock_delta": 0},
                        "reject": {"status": "rejected", "orders": 0, "stock_delta": 0},
                        "replay": {"status": "approved", "orders": 1, "stock_delta": -(i%3+1)},
                        "wrong_buyer": {"error": "OWNER_MISMATCH", "orders": 0},
                        "wrong_session": {"error": "OWNER_MISMATCH", "orders": 0},
                        "bad_hash": {"error": "SNAPSHOT_MISMATCH", "orders": 0},
                        "expired": {"error": "CONFIRMATION_EXPIRED", "orders": 0},
                        "stock_changed": {"error": "INSUFFICIENT_STOCK", "orders": 0},
                        "price_changed": {"error": "PRICE_CHANGED", "orders": 0},
                        "currency_changed": {"error": "PRICE_CHANGED", "orders": 0},
                    }
                    life = {"same_save": "same_version", "stale_save": "BuyerSkillConflict", "reopen": "same_content",
                            "delete": "LookupError", "wrong_owner": "LookupError", "tamper": "ValueError",
                            "revoke": "CapabilityError", "unreviewed": "CapabilityError",
                            "tool_escalation": "CapabilityError", "session_changed": "CapabilityVersionChanged"}
                    case["expected"] = expected[family] if group == "safety" else {"result": life[family]}
                    if group == "safety":
                        # 被拒绝的调用不能偷偷扣库存或改变确认状态。
                        case["expected"].setdefault("stock_delta", -(i+4) if family == "stock_changed" else 0)
                        case["expected"]["confirmation_status"] = case["expected"].get("status", "pending")
                    case["rationale"] = "调用生产存储及事务接口，在独立数据库断言结果；不使用模型判分。"
                cases.append(case)
    return {"schema_version": "skill-eval-v2", "version": "20261003-600-v1", "groups": GROUPS,
            "repetitions": 3, "split_policy": "每类前4个家族开发、后6个家族留出；同家族10变体不跨集合",
            "scope": "400个真实模型只读检索/Skill参考执行用例，200个真实SQLite审批/版本流程用例。合成数据，不等于生产收益。",
            "limits": ["60个场景家族、600条参数化用例；统计按家族聚合，不能当作600个独立样本。",
                       "多语种保留中文商品词；不代表商品目录翻译评测。", "旧12条已查看失败的用例保留v1作为历史诊断，不加入新留出集。",
                       "候选引用预读模式检测正文执行，目录检索命中需另行评测。", "数据结构校验通过不代表模型通过；未执行项不记通过。"],
            "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/learning/skill-cases-v2.json"))
    args = parser.parse_args()
    args.output.write_text(json.dumps(build_suite(), ensure_ascii=False, indent=2)+"\n")
    print(f"已生成 {args.output}：6类 × 100条 = 600条")


if __name__ == "__main__":
    main()
