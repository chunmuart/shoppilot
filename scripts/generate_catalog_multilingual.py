"""在 v2 后追加多语言合成目录；不改写旧商品、订单引用或旧评测底座。"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from scripts.catalog_multilingual_data import (
    FAMILY_SPECS, PLATFORM_LANGUAGES, LONG_TAIL, localized_family,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data/catalog-v3.jsonl'
LANGUAGE_ORDER = ('en', 'es', 'de', 'fr', 'ja', 'nl', 'pl', 'sv', 'it', 'pt')
CURRENCIES = ('CNY', 'USD', 'EUR', 'JPY', 'SGD')
# 模拟换算比例用于生成独立报价，不表示实时汇率；交易计价仍读取业务汇率表。
RATES = {'CNY': 1, 'USD': 7.1, 'EUR': 7.8, 'JPY': .048, 'SGD': 5.3}
# 长宽高为模拟包装尺寸，按品类定义，不沿用一套尺寸填充所有商品。
DIMENSIONS = [(45,28,16),(30,20,8),(30,25,10),(5,4,3),(20,18,8),(14,7,2),(10,4,2),
              (25,8,8),(15,10,10),(35,20,20),(30,25,6),(35,25,20),(12,10,10),
              (18,12,8),(20,16,15),(20,15,4),(35,15,10),(25,20,5),(21,15,2),
              (45,28,28),(22,20,18),(25,20,4),(12,5,5),(22,15,8),(35,12,12)]


def build_records() -> list[dict]:
    records = [json.loads(line) for line in (ROOT / 'data/catalog-v2.jsonl').read_text().splitlines() if line.strip()]
    for platform_index, (platform, languages) in enumerate(PLATFORM_LANGUAGES.items()):
        for language, locale in languages.items():
            language_index = LANGUAGE_ORDER.index(language)
            for family, (category, material, values, unit, weight) in enumerate(FAMILY_SPECS):
                text = localized_family(language, family)
                for tier, value in enumerate(values):
                    # 编号由固定平台/语种/型号决定，增加语种不会让已有 ID 漂移。
                    number = 10000 + platform_index * 10000 + language_index * 1000 + family * 4 + tier
                    pid = f'P{number}'
                    model = f'ML-{family+1:02d}-{tier+1}'
                    detail = f'{text["feature"]}: {value} {unit}'.strip()
                    state = (family * 4 + tier + platform_index + language_index) % 10
                    restricted = (family * 4 + tier + platform_index + language_index) % 11 == 0
                    currency = CURRENCIES[(family + tier + platform_index + language_index) % 5]
                    major = round((69 + family * 13 + tier * 75 + platform_index * 3) / RATES[currency], 0 if currency == 'JPY' else 2)
                    records.append({
                        'product_id': pid,
                        'title': f'Lingua {text["name"]} — {detail} · {model}',
                        'brand': 'Lingua', 'category': category, 'origin_country': 'CN',
                        'description': f'{text["purpose"]} {detail}. {text["material_label"]}: {text["material"]}. {text["model_label"]}: {model}.',
                        'highlights': [
                            {'label': text['feature'], 'detail': f'{value} {unit}'.strip()},
                            {'label': text['material_label'], 'detail': text['material']},
                        ],
                        'ships_to': ['JP', 'SG'] if restricted else ['CN', 'US', 'EU', 'JP', 'SG'],
                        'skus': [{
                            'sku_id': f'{pid}-S{variant+1}',
                            'spec': f'{text["colors"][variant]} / {detail}',
                            'price_major': round(major * (1 if variant == 0 else 1.04), 0 if currency == 'JPY' else 2),
                            'currency': currency,
                            'stock': 0 if state == 0 or (state == 1 and variant == 0) else 15 + (family * 3 + tier + variant) % 40,
                        } for variant in range(2)],
                        'source_platform': platform, 'source_language': language, 'source_locale': locale,
                        'localized_category': text['category'], 'localized_material': text['material'],
                        'external_product_id': f'demo-{platform}-{locale}-{pid}',
                        'canonical_product_id': f'CAN-{model}', 'material_tags': [material],
                        'weight_kg': round(float(value)/1000 if family == 2 else weight * (1 + tier*.12), 3),
                        'dimensions_cm': dict(zip(('length','width','height'), DIMENSIONS[family])),
                        'tax_category': category, 'rating_summary': {'average': 4.0 + tier/10, 'review_count': 20 + family * 7},
                        'updated_at': '2026-09-18', 'data_provenance': 'synthetic',
                        'model_spec': {'feature': text['feature'], 'value': value, 'unit': unit},
                        'language_group': 'long_tail' if language in LONG_TAIL or (platform == 'walmart' and language == 'fr') else 'common',
                        'evaluation_family': family, 'evaluation_tier': tier,
                        'evaluation_tags': ['multilingual', 'hard_negative' if tier < 2 else 'high_spec'],
                    })
    return records


def main():
    records = build_records()
    OUTPUT.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records))
    print(json.dumps({'products': len(records), 'skus': sum(len(r['skus']) for r in records),
                      'platforms': dict(Counter(r['source_platform'] for r in records)),
                      'new_language_groups': len({(r['source_platform'], r['source_language']) for r in records[1000:]})}, ensure_ascii=False))


if __name__ == '__main__':
    main()
