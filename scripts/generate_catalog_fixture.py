# -*- coding: utf-8 -*-
"""生成可复现的准生产演示商品集（1000 SPU / 1705 SKU）。

这是一次性数据构建工具，运行后得到应提交到仓库的 ``data/catalog-v2.jsonl``；
运行时只读取 JSONL，业务代码不再依赖硬编码商品表。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.persistence.seed_products import _build_legacy_seed_products


_OUT = Path(__file__).resolve().parents[1] / "data" / "catalog-v2.jsonl"
_PLATFORMS = ("amazon", "ebay", "etsy", "walmart")
_CURRENCIES = ("CNY", "USD", "EUR", "JPY", "SGD")
_CATEGORIES = (
    ("旅行装备", "旅行收纳 轻便 出行", "旅行收纳包"),
    ("数码配件", "数码 快充 便携", "数码扩展坞"),
    ("家居生活", "家居 天然材质 轻器物", "家居收纳盒"),
    ("户外运动", "户外 防水 耐用 徒步", "户外保温杯"),
    ("美妆个护", "个护 低敏 旅行装", "旅行洗护套装"),
    ("厨房餐饮", "厨房 食品接触 便携", "便携餐盒"),
    ("办公学习", "办公 护眼 轻薄", "桌面收纳架"),
    ("母婴宠物", "母婴 宠物 安全 易清洁", "宠物出行包"),
)
_MATERIALS = (
    ("天然纤维", "帆布棉麻"),
    ("合成聚合物", "再生尼龙"),
    ("金属", "铝合金"),
    ("陶瓷", "高温陶瓷"),
    ("玻璃", "耐热玻璃"),
)
_ORIGINS = ("CN", "US", "DE", "JP", "KR", "VN", "PT", "SG")
_DESTINATIONS = (
    ["CN"], ["US"], ["EU"], ["JP"], ["SG"],
    ["CN", "US"], ["CN", "EU"], ["US", "EU", "JP"], ["CN", "US", "EU", "JP", "SG"],
)


def _legacy_record(product, index: int) -> dict:
    # “再生”不改变材料属性；记忆棉、超纤、PVC/PU 等同样不能标为天然材质。
    synthetic_markers = ("尼龙", "记忆棉", "超纤", "聚酯", "涤纶", "PVC", "PU", "塑料")
    material = "合成聚合物" if any(marker in product.description for marker in synthetic_markers) else "天然材料"
    description = product.description.replace("无塑料感", "含再生尼龙")
    highlights = [{"label": h.label, "detail": h.detail} for h in product.highlights]
    if product.product_id == "P1001":
        highlights = [
            {"label": "材质", "detail": "帆布+再生尼龙（含合成聚合物）"},
            *highlights[1:],
        ]
    return {
        "product_id": product.product_id,
        "title": product.title,
        "brand": product.brand,
        "category": product.category,
        "origin_country": product.origin_country,
        "description": description,
        "highlights": highlights,
        "ships_to": product.ships_to,
        "skus": [
            {"sku_id": sku.sku_id, "spec": sku.spec, "price_major": sku.price.to_major_units(), "currency": sku.price.currency, "stock": sku.stock}
            for sku in product.skus
        ],
        "source_platform": _PLATFORMS[index % len(_PLATFORMS)],
        "external_product_id": f"{_PLATFORMS[index % len(_PLATFORMS)]}-{product.product_id}",
        "canonical_product_id": f"CAN-LEGACY-{index:03d}",
        "material_tags": [material],
        "weight_kg": round(0.15 + (index % 20) * 0.09, 2),
        "dimensions_cm": {"length": 12 + index % 30, "width": 8 + index % 15, "height": 4 + index % 12},
        "tax_category": product.category,
        "rating_summary": {"average": round(4.0 + (index % 9) / 10, 1), "review_count": 30 + index * 11},
        "updated_at": "2026-08-01",
    }


def _price(index: int, currency: str) -> float:
    base = (39, 89, 169, 329, 699, 1299)[index % 6]
    if currency == "CNY":
        return float(base)
    if currency == "USD":
        return round(base / 7.1, 2)
    if currency == "EUR":
        return round(base / 7.8, 2)
    if currency == "JPY":
        return round(base / 0.048)
    return round(base / 5.3, 2)


def _generated_record(index: int) -> dict:
    category, keywords, noun = _CATEGORIES[index % len(_CATEGORIES)]
    material_tag, material_text = _MATERIALS[index % len(_MATERIALS)]
    platform = _PLATFORMS[index % len(_PLATFORMS)]
    currency = _CURRENCIES[index % len(_CURRENCIES)]
    product_id = f"P{2000 + index:04d}"
    multi_sku = index < 200
    fully_out = index < 50
    partially_out = 50 <= index < 100
    skus = []
    for variant in range(2 if multi_sku else 1):
        stock = 0 if fully_out or (partially_out and variant == 0) else 8 + (index * 7 + variant * 11) % 180
        skus.append(
            {
                "sku_id": f"{product_id}-S{variant + 1}",
                "spec": ("标准版" if variant == 0 else "升级版"),
                "price_major": round(_price(index + variant, currency) * (1 if variant == 0 else 1.12), 2),
                "currency": currency,
                "stock": stock,
            },
        )
    title = f"Atlas {noun} {index:03d}"
    brand = f"Atlas-{index % 24:02d}"
    description = f"{keywords} {material_text} 多场景使用 轻量 耐用 评测候选 {index:03d}"
    # 为“无合成聚合物旅行三件套”保留一个真实可检索的天然材质正例，
    # 避免把含再生尼龙的 P1001 错标为“无塑料”。
    if index == 120:
        title = "PureCanvas 纯棉旅行三件套（收纳袋+颈枕+眼罩）"
        brand = "PureCanvas"
        description = "旅行三件套 纯棉 帆布 天然材质 不含合成聚合物 长途飞行 评测候选"

    evaluation_tags = ["hard_negative"] if index < 75 else []
    if evaluation_tags:
        description += " 标题近似款：关键属性故意不匹配，用于检验属性过滤而非标题碰撞。"

    return {
        "product_id": product_id,
        "title": title,
        "brand": brand,
        "category": category,
        "origin_country": _ORIGINS[index % len(_ORIGINS)],
        "description": description,
        "highlights": [
            {"label": "材质", "detail": material_text},
            {"label": "测试属性", "detail": f"候选分组 {index // 5}"},
        ],
        "ships_to": _DESTINATIONS[index % len(_DESTINATIONS)],
        "skus": skus,
        "source_platform": platform,
        "external_product_id": f"{platform}-{product_id}",
        "canonical_product_id": f"CAN-{index // 5:03d}",
        "material_tags": [material_tag],
        "weight_kg": round(0.1 + (index % 35) * 0.08, 2),
        "dimensions_cm": {"length": 10 + index % 31, "width": 7 + index % 17, "height": 3 + index % 13},
        "tax_category": category,
        "rating_summary": {"average": round(3.8 + (index % 12) / 10, 1), "review_count": 20 + index * 13},
        "updated_at": f"2026-08-{1 + index % 28:02d}",
        "evaluation_tags": evaluation_tags,
    }


# 每行是一种商品，不同档位有真实的功能差异；四个平台卖同一型号时共享 canonical ID。
# 全部为合成演示数据，品牌、规格、价格均不代表平台真实在售商品。
# category, noun, material, purpose, four specification tiers
_EXPANDED_FAMILIES = (
    ("旅行装备", "登山背包", "合成聚合物", "山路步行时背负装备，有胸带和腰带分担肩部重量", ("15L 无防雨罩", "25L 无防雨罩", "35L 配防雨罩", "45L 配防雨罩及独立水袋仓")),
    ("旅行装备", "航空颈枕", "天然纤维", "坐着休息时稳定头部，外套为可拆洗纯棉", ("无侧向支撑", "单侧支撑", "双侧支撑", "双侧支撑且前部有托下巴结构")),
    ("旅行装备", "行李箱", "金属", "机场转机拖行，铝合金箱体配万向轮", ("28寸 托运尺寸", "24寸 托运尺寸", "20寸 仅密码锁", "20寸 TSA锁静音轮")),
    ("旅行装备", "压缩收纳袋", "合成聚合物", "行李内分开衣物，拉链可重复开合", ("2件 无压缩层", "3件 无压缩层", "4件 双层压缩", "6件 双层压缩干湿分离")),
    ("数码配件", "氮化镓充电器", "合成聚合物", "出差给便携电脑和手机补电，支持USB-C PD", ("30W 单口", "45W 双口", "65W 双口", "100W 三口全球插脚")),
    ("数码配件", "蓝牙耳机", "合成聚合物", "乘地铁听播客，麦克风支持语音通话", ("仅通话降噪 20小时", "仅通话降噪 30小时", "主动降噪35dB 40小时", "主动降噪45dB 50小时可折叠")),
    ("数码配件", "USB-C扩展坞", "金属", "笔记本连接外接显示器和有线网络，铝合金外壳", ("HDMI1080P 无网口", "HDMI4K30Hz 无网口", "HDMI4K60Hz 千兆网口", "双HDMI4K60Hz 千兆网口100W回充")),
    ("数码配件", "移动电源", "合成聚合物", "离开插座后为手机供电，内置锂电池", ("5000mAh 10W", "10000mAh 18W", "20000mAh 30W", "20000mAh 65W自带USB-C线")),
    ("家居生活", "遮光窗帘", "天然纤维", "日间睡眠减少窗外光线，纯棉面料可机洗", ("遮光率50%", "遮光率70%", "遮光率90%", "遮光率99%双层隔热")),
    ("家居生活", "粗陶马克杯", "陶瓷", "书桌喝咖啡，粗陶手工釉面中性色", ("200ml 无把手", "280ml 无把手", "350ml 带把手", "450ml 带把手可进洗碗机")),
    ("家居生活", "衣物收纳箱", "天然纤维", "换季整理衣柜，棉麻表层收纳衣物", ("10L 不可叠放", "20L 不可叠放", "40L 可叠放", "60L 可叠放防尘带透明窗口")),
    ("家居生活", "折叠晾衣架", "金属", "阳台晒衣物，铝合金支撑架可折叠", ("承重5kg", "承重10kg", "承重20kg", "承重30kg可伸缩带轮")),
    ("户外运动", "露营灯", "合成聚合物", "帐篷和营地照明，Type-C充电", ("100流明 不防水", "200流明 IPX4", "400流明 IPX5", "800流明 IPX6磁吸挂钩")),
    ("户外运动", "登山杖", "金属", "上下坡借力减轻膝盖负担，铝合金杖身", ("450g 不可折叠", "350g 三节伸缩", "280g 三节折叠", "220g 五节折叠快锁")),
    ("户外运动", "羽绒睡袋", "合成聚合物", "户外过夜保暖，填充鸭绒外层尼龙", ("舒适温15℃", "舒适温5℃", "舒适温0℃", "舒适温-10℃防风帽")),
    ("户外运动", "真空保温壶", "金属", "徒步带热水，316不锈钢内胆", ("350ml 保温4小时", "500ml 保温6小时", "750ml 保温12小时", "1000ml 保温24小时带提手")),
    ("美妆个护", "电动剃须刀", "金属", "出差整理胡须，可USB充电", ("单刀头不可水洗", "双刀头刀头可水洗", "三刀头全身水洗", "三刀头全身水洗带鬓角修剪器")),
    ("美妆个护", "洗漱分装瓶", "玻璃", "洗护液分装携带，玻璃瓶体避免吸附气味", ("150ml 普通盖", "100ml 普通盖", "80ml 防漏旋盖", "60ml 防漏旋盖带标签四只装")),
    ("美妆个护", "旅行吹风机", "合成聚合物", "酒店洗头后吹干头发，可折叠手柄", ("600W 单电压", "1000W 单电压", "1200W 双电压", "1600W 双电压负离子")),
    ("美妆个护", "洁面巾", "天然纤维", "洗脸后擦拭水分，纯棉无香料", ("30抽薄款", "50抽薄款", "80抽加厚", "100抽加厚独立包装")),
    ("厨房餐饮", "密封餐盒", "玻璃", "上班带饭，硼硅玻璃盒体", ("400ml 不可微波", "600ml 可微波无分隔", "900ml 可微波双分隔", "1200ml 可微波三分隔防漏")),
    ("厨房餐饮", "便携餐具", "金属", "露营或午餐使用，可反复清洗", ("不锈钢勺单件", "不锈钢叉勺两件", "钛合金叉勺两件", "钛合金筷叉勺三件带收纳盒")),
    ("厨房餐饮", "手冲咖啡壶", "金属", "控制细水流冲泡咖啡，不锈钢细嘴", ("300ml 无温度显示", "500ml 无温度显示", "700ml 温度显示", "900ml 温度显示电加热控温")),
    ("厨房餐饮", "食品保鲜袋", "合成聚合物", "冰箱分装食材，食品级硅胶可重复使用", ("300ml 不可冷冻", "500ml 可冷冻", "1000ml 可冷冻密封", "1500ml 可冷冻密封可洗碗机")),
    ("办公学习", "阅读台灯", "金属", "书桌阅读照明，铝合金灯臂可调角度", ("单色温 300流明", "双色温 400流明", "三色温 600流明", "无级色温 800流明显色指数95")),
    ("办公学习", "笔记本支架", "金属", "抬高电脑屏幕改善桌面视线，铝合金底座", ("固定高度5cm", "两档高度10cm", "六档高度18cm", "无级升降25cm旋转底座")),
    ("办公学习", "无线鼠标", "合成聚合物", "办公控制光标，右手握持", ("仅2.4G 有声按键", "仅蓝牙 静音按键", "蓝牙2.4G双模 静音", "蓝牙2.4G双模 静音多设备切换")),
    ("办公学习", "文件收纳夹", "天然纤维", "分类整理纸张，牛皮纸内页和棉布封面", ("A5 4格", "A4 6格", "A4 12格", "A4 24格防尘拉链")),
    ("母婴宠物", "宠物航空包", "合成聚合物", "带猫出行，网面透气便于观察", ("承重3kg 单侧开门", "承重5kg 双侧开门", "承重7kg 可拆洗垫", "承重9kg 可拆洗垫四面透气")),
    ("母婴宠物", "宠物饮水器", "金属", "猫咪日常喝水，304不锈钢饮水盘", ("1L 无过滤", "1.5L 单层过滤", "2L 双层过滤", "3L 三层过滤缺水断电")),
    ("母婴宠物", "婴儿纱布浴巾", "天然纤维", "宝宝洗澡后包裹吸水，纯棉纱布", ("2层60cm", "4层80cm", "6层100cm", "8层120cm无荧光剂")),
    ("母婴宠物", "宠物梳毛刷", "金属", "梳理猫犬浮毛，不锈钢圆头梳齿", ("固定短齿", "固定长齿", "可调齿距", "可调齿距一键退毛")),
)


def _expanded_records():
    records = []
    for model_index in range(125):
        family, tier = divmod(model_index, 4)
        category, noun, material, purpose, specs = _EXPANDED_FAMILIES[family]
        model = f"GX-{family+1:02d}-{tier+1}"
        for platform_index, platform in enumerate(_PLATFORMS):
            offset = model_index*4+platform_index
            pid = f"P{3000+offset}"
            currency = _CURRENCIES[(model_index+platform_index)%5]
            base_cny = (59,99,189,329)[tier] * (1 + family%3) + platform_index*7
            factor = {"CNY":1,"USD":7.1,"EUR":7.8,"JPY":.048,"SGD":5.3}[currency]
            fully_out = offset%10 == 0
            partially_out = offset%10 == 1
            records.append({
                "product_id":pid, "title":f"Roamix {noun} {model} {specs[tier]}",
                "brand":"Roamix", "category":category, "origin_country":_ORIGINS[family%8],
                "description":f"{purpose}。本型号规格：{specs[tier]}。商品功能以本型号为准，其他档位配置不包含在内。",
                "highlights":[{"label":"规格","detail":specs[tier]},{"label":"材质","detail":material}],
                "ships_to":(["JP","SG"] if offset%11==0 else ["CN","US","EU","JP","SG"]),
                "skus":[{"sku_id":f"{pid}-S{i+1}","spec":color,"price_major":round((base_cny+i*9)/factor,2),"currency":currency,
                         "stock":0 if fully_out or (partially_out and i==0) else 15+(offset*13+i*7)%100}
                        for i,color in enumerate(("石墨黑","雾灰"))],
                "source_platform":platform,"external_product_id":f"demo-{platform}-{pid}",
                "canonical_product_id":f"CAN-{model}","material_tags":[material],
                "weight_kg":round(.15+(family%10)*.11+tier*.03,2),
                "dimensions_cm":{"length":20+tier*3,"width":12+tier,"height":8+tier},
                "tax_category":category,"rating_summary":{"average":round(4+(offset%9)/10,1),"review_count":20+offset*3},
                "updated_at":"2026-09-18","data_provenance":"synthetic","model_spec":specs[tier],
                "evaluation_tags":["hard_negative"] if tier==0 or fully_out else [],
                "evaluation_family":family,"evaluation_tier":tier,
            })
    return records


def build_records() -> list[dict]:
    legacy = [_legacy_record(product, index) for index, product in enumerate(_build_legacy_seed_products())]
    generated = [_generated_record(index) for index in range(440)]
    return legacy + generated + _expanded_records()


def main() -> None:
    records = build_records()
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text("\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n", encoding="utf-8")
    print(f"已写入 {_OUT}：{len(records)} SPU，{sum(len(record['skus']) for record in records)} SKU")


if __name__ == "__main__":
    main()
