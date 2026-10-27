"""构建独立展示媒体注册表；不修改冻结商品集、价格或工具结果。"""
from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = 'backpack packing-cubes neck-pillow charger headphones power-bank usb-hub bottle lantern sleeping-bag curtain storage-box mug glass-container kettle food-bags desk-lamp laptop-stand notebook pet-carrier pet-fountain baby-towel toiletry-bottles hair-dryer camping-stool'.split()
EXTRA = 'suitcase travel-set trekking-poles eye-mask earbuds passport-pouch luggage-scale'.split()
# 仅在构建时为已知样例目录分配概念图；运行时按注册表中的精确 ID 读取。
RULES = [
 ('travel-set', ('旅行三件套',)), ('suitcase', ('登机箱', '行李箱')),
 ('packing-cubes', ('收纳袋', '收纳套', '收纳包', '收纳立方')),
 ('neck-pillow', ('颈枕',)), ('headphones', ('头戴', '主动降噪蓝牙耳机')),
 ('earbuds', ('蓝牙耳机', '蓝牙耳塞')), ('charger', ('充电器', '转换插头')),
 ('power-bank', ('移动电源',)), ('usb-hub', ('扩展坞', '读卡器')),
 ('lantern', ('露营灯',)), ('eye-mask', ('眼罩',)), ('passport-pouch', ('证件包',)),
 ('luggage-scale', ('行李秤', '行李称')), ('trekking-poles', ('登山杖',)),
 ('pet-carrier', ('宠物出行包', '宠物背包')),
 ('backpack', ('双肩包', '背包')), ('bottle', ('保温杯', '保温瓶', '水壶')),
 ('sleeping-bag', ('睡袋',)), ('curtain', ('遮光窗帘',)), ('storage-box', ('收纳盒',)),
 ('mug', ('陶瓷杯', '马克杯')), ('glass-container', ('玻璃饭盒', '玻璃保鲜盒')),
 ('kettle', ('手冲壶',)), ('food-bags', ('保鲜袋',)), ('desk-lamp', ('台灯',)),
 ('laptop-stand', ('笔记本支架',)), ('notebook', ('笔记本', '记事本')),
 ('pet-carrier', ('宠物出行包', '宠物背包')), ('pet-fountain', ('宠物饮水',)),
 ('baby-towel', ('婴儿浴巾',)), ('toiletry-bottles', ('分装瓶',)),
 ('hair-dryer', ('吹风机',)), ('camping-stool', ('折叠凳',)),
]

def build():
    records = [json.loads(line) for line in (ROOT / 'data/catalog-v3.jsonl').read_text().splitlines() if line.strip()]
    assets = {name: {'url': f'/products/catalog-atlas-{"a" if i < 16 else "b"}-v1.png', 'cell': i % 16, 'columns': 4, 'rows': 4} for i, name in enumerate(FAMILIES + EXTRA)}
    products, skus, missing = {}, {}, []
    for record in records:
        family = record.get('evaluation_family')
        key = FAMILIES[family] if type(family) is int and 0 <= family < len(FAMILIES) else next((key for key, words in RULES if any(word in record['title'] for word in words)), None)
        # 保留已有三张专属概念图；新增图库不覆盖它们。
        original = {'P1003': 'wanderlite.png', 'P1018': 'drypack.png', 'P1049': 'budgetpack.png'}.get(record['product_id'])
        if original:
            key = record['product_id']
            assets[key] = {'url': '/products/' + original}
        if key:
            products[record['product_id']] = key
            skus.update({sku['sku_id']: record['product_id'] for sku in record['skus']})
        else:
            missing.append(record['product_id'])
    manifest = {'version': '2026-10-10-v1', 'kind': 'illustration', 'assets': assets, 'products': products, 'skus': skus}
    (ROOT / 'app/presentation/assets/product-media-v1.json').write_text(json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))+'\n')
    return {'catalog_total': len(records), 'covered': len(products), 'uncovered': len(missing), 'missing_product_ids': missing, 'families': dict(Counter(products.values()))}

if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))
