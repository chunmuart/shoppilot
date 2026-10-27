"""冻结千件合成目录上的人工意图模板；运行评测前生成，不根据排序结果改金标。"""

from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 意图由人可读规则定义，避免把商品标题复制为查询后声称语义检索提高。
_INTENTS = (
    (
        "周末走山路，想把帐篷衣服背身上，肩膀别太累",
        "走山路要带饮水系统，至少45升，还要防雨",
    ),
    (
        "飞机上坐着打盹总是点头，想找能托住脑袋的东西",
        "长途坐车要两边扶住头，同时能托下巴",
    ),
    ("机场中转要拖着走的箱子，轮子得灵活", "要20寸带TSA锁且轮子安静的登机箱"),
    ("出门衣服塞不进行李箱，想分类后压小一点", "行李整理要六件，能压缩并把湿衣服分开"),
    ("出差给电脑手机补电，想少带几个电源头", "电脑手机一起充，要100瓦三口和全球插脚"),
    ("地铁太吵，想听清播客，不想总调大音量", "耳机要主动降噪45分贝和至少50小时续航"),
    (
        "轻薄电脑插口不够，想连显示器再接网线",
        "电脑要接两台4K60显示器和千兆网线，还得100瓦回充",
    ),
    ("出门拍照手机下午就没电，找能放包里补电的", "移动电源要两万毫安时65瓦且自带C口线"),
    ("我上夜班白天睡，窗外太阳晒进来太亮", "窗帘要99%遮光，还要双层隔热，纯棉"),
    ("桌上喝拿铁用的杯子，想要质朴中性色的", "咖啡杯要450毫升带把手，能进洗碗机"),
    ("换季衣服堆在柜子里，找能归拢起来的", "收纳箱要60升能叠放，防灰且能看见里面"),
    ("阳台空间小，晒完衣服想把架子收起来", "晾衣架至少承重30公斤，能伸长还要带轮"),
    (
        "晚上帐篷里做饭看不清，想找可充电的照明",
        "营地灯要800流明，IPX6，能吸在金属上挂起来",
    ),
    ("下山膝盖吃力，想要能撑着走的轻量装备", "登山杖单支220克，五节折叠带快锁"),
    ("野外过夜有点冷，找能钻进去保暖的装备", "睡袋舒适温要到零下10度，还要防风帽"),
    ("冬天徒步想一路喝到热水，找个耐用容器", "水壶要一升，24小时保温，316内胆带提手"),
    ("出差几天胡茬长了，找能随身用的电动工具", "剃须刀三个刀头，全身水洗，还要修鬓角"),
    (
        "洗发水大瓶太占地方，出行想分小份且不漏",
        "洗护分装要60毫升玻璃瓶，旋盖防漏且有标签四只装",
    ),
    ("酒店洗完头想自己吹干，需要能塞进行李的", "吹风机要双电压1600瓦，还要负离子"),
    ("洗脸之后不用毛巾，想找没香味的纯棉擦脸用品", "洁面巾要100抽加厚，独立包装"),
    ("上班自己带饭，想找玻璃的饭菜容器", "饭盒要1200毫升三分隔防漏，能微波"),
    ("野餐不想用一次性叉勺，找能反复洗的轻餐具", "要钛的筷子叉子勺子三件，还要收纳盒"),
    ("手冲咖啡水流太粗难控制，想找细嘴容器", "手冲壶要900毫升，有温度显示和电加热控温"),
    ("剩下的食材想小份放冰箱，容器能反复用", "保鲜袋要1500毫升密封，能冷冻和进洗碗机"),
    (
        "晚上桌上看书光线暗，想照亮书页且可调角度",
        "台灯要800流明，显色指数95，色温无级调节",
    ),
    ("电脑屏幕太低看得脖子酸，想抬高桌面设备", "电脑支架要25厘米无级升降，底座能转"),
    (
        "办公室操作电脑不想让按键声音吵到同事",
        "鼠标要静音，蓝牙和2.4G双模，能切多个设备",
    ),
    ("一堆A4票据要分类，不想翻遍整个包", "文件夹要A4二十四格，还要防尘拉链"),
    ("想带猫出门，既能装进去也方便观察它", "航空包承重9公斤，垫子能拆洗，四面透气"),
    ("猫平时喝水少，想试试循环水而不是普通碗", "饮水器3升不锈钢盘，三层过滤且缺水断电"),
    ("宝宝洗澡后想裹起来吸水，用柔软棉织物", "宝宝浴巾要纯棉八层120厘米，不加荧光剂"),
    ("家里猫狗掉毛，想用圆头齿梳理浮毛", "宠物毛刷要不锈钢圆头短齿"),
)


def build_cases(records):
    cases = []
    for family, queries in enumerate(_INTENTS):
        group = [r for r in records if r.get("evaluation_family") == family]
        split = "dev" if family % 4 == 0 else "holdout"
        for kind, query in zip(("intent", "specification"), queries):
            selected = [
                r
                for r in group
                if (
                    (
                        kind == "intent"
                        and r["evaluation_tier"] >= ({3: 2, 5: 2, 6: 2, 17: 2, 26: 1, 27: 1}.get(family, 0))
                    )
                    or r["evaluation_tier"] == (3 if family < 31 else 0)
                )
                and "CN" in r["ships_to"]
                and any(s["stock"] > 0 for s in r["skus"])
            ]
            cases.append(
                {
                    "id": f"v2-{family:02d}-{kind}",
                    "split": split,
                    "family": family,
                    "kind": kind,
                    "query": "在 Roamix 系列中，" + query,
                    "ship_to": "CN",
                    "target_currency": "CNY",
                    "relevant": sorted(r["product_id"] for r in selected),
                    "relevant_canonical_ids": sorted(
                        {r["canonical_product_id"] for r in selected}
                    ),
                    "expected_empty": not selected,
                    "gold_rule": "同类用途"
                    if kind == "intent"
                    else "全部明示规格必须同时满足",
                    "provenance": "人工意图模板 + 冻结目录事实校验，未调用LLM生成/评分",
                }
            )
    # 不可能满足的硬约束单独统计，不能用空命中获得高召回分。
    for i, category in enumerate(
        (
            "旅行装备",
            "数码配件",
            "家居生活",
            "户外运动",
            "美妆个护",
            "厨房餐饮",
            "办公学习",
            "母婴宠物",
        )
    ):
        cases.append(
            {
                "id": f"v2-empty-{i}",
                "split": "dev" if i % 4 == 0 else "holdout",
                "family": f"empty-{i}",
                "kind": "empty",
                "query": f"找{category}商品，预算只有1元",
                "category": category,
                "price_max_major": 1,
                "ship_to": "CN",
                "target_currency": "CNY",
                "expected_empty": True,
                "relevant": [],
                "relevant_canonical_ids": [],
                "gold_rule": "所有可售SKU换算后超过1元",
            }
        )
    return cases


def main():
    source = ROOT / "data/catalog-v2.jsonl"
    cases = build_cases([json.loads(l) for l in source.read_text().splitlines()])
    target = ROOT / "eval/v2/product_retrieval.jsonl"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        "".join(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n" for c in cases)
    )
    print(
        json.dumps(
            {
                "cases": len(cases),
                "splits": dict(Counter(c["split"] for c in cases)),
                "catalog_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
