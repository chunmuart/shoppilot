"""固定人工查询与语种切片；查询不从商品标题拼接，金标使用结构化事实。"""
import json
from pathlib import Path
from scripts.catalog_multilingual_data import PLATFORM_LANGUAGES

ROOT = Path(__file__).resolve().parents[1]
# 仅测明确的商品类别意图，不把未说出的容量偏好伪造为唯一正确档位。
FAMILIES = (0, 3, 4, 10, 12, 17, 19, 23)
QUERIES = {
'zh': '日间徒步背装备的双肩包|给设备充电的USB-C充电器|在嘈杂列车上听音乐的耳机|让卧室暗下来的窗帘|喝咖啡用的陶瓷杯|把笔记本电脑架高的支架|携带小宠物出行的包|旅行带的吹风机',
'en': 'Backpack for a day hike|USB-C charger for my devices|Headphones to listen on a noisy train|Curtains to darken a bedroom|A ceramic cup for coffee|A stand to raise my laptop|A bag to carry my small pet|A hair dryer to take on holiday',
'es': 'Mochila para una excursión de un día|Cargador USB-C para mis dispositivos|Auriculares para un tren ruidoso|Cortinas para oscurecer el dormitorio|Una taza de cerámica para el café|Un soporte para elevar mi portátil|Una bolsa para llevar a mi mascota|Un secador de pelo para las vacaciones',
'de': 'Rucksack für eine Tageswanderung|USB-C-Ladegerät für meine Geräte|Kopfhörer für einen lauten Zug|Vorhang zum Abdunkeln des Schlafzimmers|Eine Keramiktasse für Kaffee|Ein Ständer zum Erhöhen meines Laptops|Eine Tasche zum Transport meiner Katze|Ein Haartrockner für den Urlaub',
'fr': 'Un sac à dos pour une randonnée à la journée|Un chargeur USB-C pour mes appareils|Un casque pour écouter dans un train bruyant|Des rideaux pour assombrir la chambre|Une tasse en céramique pour le café|Un support pour surélever mon ordinateur portable|Un sac pour transporter mon petit animal|Un sèche-cheveux à emporter en vacances',
'ja': '日帰り登山に使うリュック|機器を充電するUSB-C充電器|電車で音楽を聴くヘッドホン|寝室を暗くするカーテン|コーヒー用の陶器のカップ|ノートパソコンを高くする台|小型ペットを運ぶバッグ|旅行に持っていくドライヤー',
'nl': 'Rugzak voor een dagwandeling|USB-C-oplader voor mijn apparaten|Koptelefoon voor een lawaaierige trein|Gordijnen om de slaapkamer donker te maken|Een keramische mok voor koffie|Een standaard om mijn laptop hoger te zetten|Een tas om mijn kleine huisdier te vervoeren|Een föhn om mee te nemen op vakantie',
'pl': 'Plecak na jednodniową wędrówkę|Ładowarka USB-C do moich urządzeń|Słuchawki do słuchania w głośnym pociągu|Zasłony do zaciemnienia sypialni|Ceramiczny kubek do kawy|Podstawka do podniesienia laptopa|Torba do przewożenia małego zwierzęcia|Suszarka do włosów na wyjazd',
'sv': 'Ryggsäck för en dagsvandring|USB-C-laddare för mina enheter|Hörlurar för ett bullrigt tåg|Gardiner för att göra sovrummet mörkt|En keramikmugg för kaffe|Ett ställ för att höja min laptop|En väska för att transportera mitt lilla husdjur|En hårtork att ta med på semestern',
'it': 'Zaino per una escursione giornaliera|Caricatore USB-C per i miei dispositivi|Cuffie per ascoltare su un treno rumoroso|Tende per oscurare la camera|Una tazza in ceramica per il caffè|Un supporto per alzare il portatile|Una borsa per trasportare il mio piccolo animale|Un asciugacapelli da portare in vacanza',
'pt': 'Mochila para uma caminhada de um dia|Carregador USB-C para os meus dispositivos|Auscultadores para ouvir num comboio ruidoso|Cortinas para escurecer o quarto|Uma caneca de cerâmica para café|Um suporte para elevar o meu portátil|Uma bolsa para transportar o meu animal pequeno|Um secador de cabelo para levar de férias',
}


def build_cases(records):
    cases = []
    for platform, languages in PLATFORM_LANGUAGES.items():
        for language in languages:
            pool = [r for r in records if r['source_platform'] == platform and r.get('source_language') == language]
            assert len(pool) == 100
            for query_language in ([language, 'zh'] if language == 'en' else [language, 'en', 'zh']):
                mode = 'monolingual' if language == query_language else 'cross_language'
                for index, family in enumerate(FAMILIES):
                    gold = [r['product_id'] for r in pool if r['evaluation_family'] == family and 'CN' in r['ships_to'] and any(s['stock'] > 0 for s in r['skus'])]
                    assert gold
                    cases.append({
                        'id': f'ml-{platform}-{language}-{query_language}-{family:02d}',
                        'platform': platform, 'corpus_language': language, 'query_language': query_language,
                        'mode': mode, 'family': family, 'query': QUERIES[query_language].split('|')[index],
                        'ship_to': 'CN', 'relevant_product_ids': gold,
                        'relevant_canonical_ids': sorted({r['canonical_product_id'] for r in pool if r['product_id'] in gold}),
                    })
    return cases


def main():
    records = [json.loads(line) for line in (ROOT/'data/catalog-v3.jsonl').read_text().splitlines()]
    path = ROOT/'eval/v3/multilingual_retrieval.jsonl'
    path.parent.mkdir(exist_ok=True)
    cases = build_cases(records)
    path.write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in cases))
    print(json.dumps({'cases':len(cases), 'monolingual':sum(c['mode']=='monolingual' for c in cases), 'cross_language':sum(c['mode']=='cross_language' for c in cases)}))


if __name__ == '__main__':
    main()
