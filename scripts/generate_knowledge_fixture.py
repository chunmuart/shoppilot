# -*- coding: utf-8 -*-
"""生成带来源/时效元数据的离线评测知识库语料。"""
from __future__ import annotations

import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_KNOWLEDGE = _ROOT / "knowledge"
_MANIFEST = _KNOWLEDGE / "manifest.jsonl"
_CATEGORIES = (
    ("travel-gear", "旅行装备", "收纳、舒适与行李组织"),
    ("digital-accessories", "数码配件", "供电、音频与连接"),
    ("home-living", "家居生活", "材质、器物与易碎运输"),
    ("outdoor-sports", "户外运动", "防护、重量与环境适配"),
    ("beauty-care", "美妆个护", "成分、低敏与旅行分装"),
    ("kitchen-dining", "厨房餐饮", "食品接触、保温与清洁"),
    ("office-study", "办公学习", "护眼、收纳与跨境供电"),
    ("baby-pet", "母婴宠物", "安全、尺寸与可清洁性"),
)
_REGIONS = ("US", "EU", "JP", "SG", "CN")


def _long_body(category: str, focus: str, variant: str) -> str:
    dimensions = (
        ("场景", "先把使用场景拆成携带频率、接触对象和使用时长；{category}{variant}只讨论{focus}，不把其他品类的经验直接套用。"),
        ("预算", "{category}{variant}需要分别记录商品标价、目标币种、运费和税费，预算不足时应指出是哪一项造成缺口。"),
        ("材质", "核对{category}{variant}的材质标签与实际接触部位；营销词不能替代对{focus}相关属性的可验证说明。"),
        ("规格", "对{category}{variant}比较重量、尺寸、容量或功率时，应保留单位和 SKU 规格，避免把不同版本混成同一结论。"),
        ("库存", "{category}{variant}的默认规格可能缺货，推荐前应检查可售 SKU；无库存不是可用候选，只能作为替代说明。"),
        ("配送", "{category}{variant}的配送范围要按目的国核验；没有覆盖该地区时，不能根据相近国家的规则推断可送达。"),
        ("风险", "遇到{category}{variant}的过敏、易碎、禁运或安全问题，应列出缺失证据并要求通过实时工具或商品页确认。"),
        ("时效", "{category}{variant}资料的更新时间影响判断；当资料早于本次评测快照时，应提示用户结果可能过期。"),
        ("比较", "{category}{variant}的候选排序应说明各自满足了哪些{focus}条件，不能只因为标题相似就宣称同样适合。"),
        ("表达", "回答{category}{variant}问题时，区分已验证事实、推测和待确认事项；价格与配送数字只能引用当次工具结果。"),
        ("复核", "在给出{category}{variant}结论前复核目的地、币种和 SKU 是否一致；任一条件变化都应重新计算而非沿用旧答案。"),
        ("边界", "{category}{variant}快照用于离线评测，不替代商品说明或监管公告；涉及{focus}的强断言必须附带来源和有效期。"),
        ("替代", "当{category}{variant}没有完全满足{focus}的商品时，应给出约束不满足的原因，而不是把近似候选包装成等价替代。"),
        ("证据", "{category}{variant}的每个关键结论应能回指材质、尺寸、库存、配送或报价字段；缺少证据时只允许提出待确认建议。"),
        ("优先级", "买家同时提出预算和安全限制时，{category}{variant}应先满足不可妥协的{focus}约束，再解释可调节的偏好。"),
        ("版本", "记录{category}{variant}使用的文档版本和商品更新时间，避免把历史{focus}结论混入本次推荐。"),
        ("地域", "{category}{variant}的地区规则必须与收货地逐项对应；GLOBAL 指引不能直接替代针对{focus}的区域性证据。"),
        ("不确定性", "如果{category}{variant}缺少判断{focus}所需字段，回复要明确说明未知项及补充信息，而不是以常识补齐。"),
        ("交叉", "{category}{variant}与其他品类共同决策时，分别列出各自的{focus}条件，避免一篇文档替另一篇文档做结论。"),
        ("复盘", "遇到用户追问时，重新展示{category}{variant}已验证的{focus}证据和未验证条件，保证多轮回答没有悄悄改变口径。"),
        ("来源", "{category}{variant}引用外部资料时要标出来源类型和发布日期；没有可靠来源的{focus}信息只能作为待核实线索。"),
        ("量化", "比较{category}{variant}候选时，以统一单位量化重量、容量、功率或价格，避免用模糊形容词代替{focus}判断。"),
        ("例外", "{category}{variant}中存在例外条件时，应把触发条件写清楚；不能因为多数商品符合{focus}就默认所有商品都符合。"),
        ("筛选", "先以{category}{variant}的硬约束缩小候选，再比较{focus}偏好；筛选顺序应能从工具事件和字段值复现。"),
        ("沟通", "向用户解释{category}{variant}取舍时，说明哪个{focus}条件导致排除，帮助用户决定是否放宽限制。"),
        ("异常", "当{category}{variant}工具返回为空、超时或字段不完整时，回复应报告异常并停止对{focus}做确定性推荐。"),
        ("一致", "确保{category}{variant}的商品卡、到手价和最终回复使用同一币种与 SKU，避免{focus}结论在不同环节矛盾。"),
        ("维护", "{category}{variant}文档更新后应重新验证涉及{focus}的评测金标，防止旧样本继续奖励过期行为。"),
        ("升级", "当{category}{variant}问题超出合成快照范围时，建议升级到实时商品或权威来源核验，而不是扩写未经证实的{focus}细节。"),
        ("记录", "保留{category}{variant}推荐采用的关键字段和理由，使后续评测能检查{focus}判断是否来自可追溯证据。"),
    )
    paragraphs = [
        f"# {category}{variant}评测知识快照",
        "",
        "## 适用范围",
        f"本文用于 ShopPilot 离线评测中的{category}问题，重点覆盖{focus}。内容是合成的演示快照，不能替代实时商品说明、法律意见或监管机构公告。",
        "",
        "## 判断卡片",
    ]
    for index, (title, template) in enumerate(dimensions, start=1):
        paragraphs += [f"### {index}. {title}", template.format(category=category, variant=variant, focus=focus), ""]
    paragraphs += [
        "## 回复边界",
        f"当{category}{variant}文档与实时商品字段冲突时，以实时字段为准，并解释{focus}中仍无法确定的部分。",
    ]
    return "\n".join(paragraphs) + "\n"


def _entry(filename: str, document_id: str, *, region: str, topic: str) -> dict:
    return {
        "document_id": document_id,
        "filename": filename,
        "source": "ShopPilot 离线评测知识快照（合成演示，不用于实时法规结论）",
        "source_type": "synthetic_evaluation_fixture",
        "published_at": "2026-08-01",
        "effective_from": "2026-08-01",
        "effective_to": "2026-12-31",
        "region": region,
        "version": "2026.08-eval-v1",
        "topic": topic,
    }


def build_manifest() -> list[dict]:
    entries: list[dict] = []
    for filename in sorted(path.name for path in _KNOWLEDGE.glob("*.md") if not path.name.startswith("eval-")):
        # 历史的跨境通则包含关税/免税/限制等政策性说法，必须走来源和有效期门禁。
        topic = "policy" if filename == "cross-border-guide.md" else "category"
        entries.append(_entry(filename, Path(filename).stem, region="GLOBAL", topic=topic))
    for slug, category, focus in _CATEGORIES:
        for variant in ("概览", "参数判断", "价格与预算", "避坑与合规"):
            filename = f"eval-{slug}-{variant}.md"
            path = _KNOWLEDGE / filename
            path.write_text(_long_body(category, focus, variant), encoding="utf-8")
            entries.append(_entry(filename, Path(filename).stem, region="GLOBAL", topic="category"))
    for region in _REGIONS:
        filename = f"eval-policy-{region.lower()}.md"
        (_KNOWLEDGE / filename).write_text(
            _long_body(f"{region} 跨境规则", "申报、配送限制与报价边界", "政策演示快照"),
            encoding="utf-8",
        )
        entries.append(_entry(filename, Path(filename).stem, region=region, topic="policy"))
    for suffix, focus in (("global-shipping", "通用运费与体积重"), ("battery", "含电池商品限制"), ("material", "材质与过敏限制")):
        filename = f"eval-policy-{suffix}.md"
        (_KNOWLEDGE / filename).write_text(
            _long_body(f"跨境通用规则（{focus}）", focus, "政策演示快照"),
            encoding="utf-8",
        )
        entries.append(_entry(filename, Path(filename).stem, region="GLOBAL", topic="policy"))
    return entries


def main() -> None:
    entries = build_manifest()
    _MANIFEST.write_text("\n".join(json.dumps(entry, ensure_ascii=False, sort_keys=True) for entry in entries) + "\n", encoding="utf-8")
    print(f"已生成 {len(entries)} 篇知识文档与 {_MANIFEST}")


if __name__ == "__main__":
    main()
