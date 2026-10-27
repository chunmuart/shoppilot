"""人工编写的多语言合成商品词表；平台支持依据见检索评测报告。

common / long_tail 仅用于工程覆盖分组，不代表平台销量排名。
商品正文与评测标签分离；不请求聊天模型生成或精排。
"""

PLATFORM_LANGUAGES = {
    'amazon': {'en': 'en-US', 'es': 'es-ES', 'de': 'de-DE', 'fr': 'fr-FR', 'ja': 'ja-JP', 'nl': 'nl-NL', 'pl': 'pl-PL', 'sv': 'sv-SE'},
    'ebay': {'en': 'en-US', 'es': 'es-ES', 'de': 'de-DE', 'fr': 'fr-FR', 'it': 'it-IT', 'nl': 'nl-NL', 'pl': 'pl-PL'},
    'etsy': {'en': 'en-US', 'es': 'es-ES', 'de': 'de-DE', 'fr': 'fr-FR', 'it': 'it-IT', 'ja': 'ja-JP', 'nl': 'nl-NL', 'pl': 'pl-PL', 'pt': 'pt-PT'},
    'walmart': {'en': 'en-US', 'es': 'es-MX', 'fr': 'fr-CA'},
}
LONG_TAIL = {'nl', 'pl', 'sv', 'pt'}
# 25 类 × 4 个有业务差异的规格 = 每平台、每语种 100 件；括号中的编号不进入检索正文。
# 名称、用途与属性行按 FAMILY_SPECS 一一对应，使用分隔符降低词表维护时的歧义。
FAMILY_SPECS = [
    ('旅行装备', '合成聚合物', [20, 30, 40, 50], 'L', .6),
    ('旅行装备', '合成聚合物', [2, 4, 6, 8], '', .2),
    ('旅行装备', '合成聚合物', [120, 160, 220, 300], 'g', .12),
    ('数码配件', '合成聚合物', [30, 45, 65, 100], 'W', .1),
    ('数码配件', '合成聚合物', [0, 25, 35, 45], 'dB', .25),
    ('数码配件', '合成聚合物', [5000, 10000, 15000, 20000], 'mAh', .12),
    ('数码配件', '金属', [3, 5, 7, 9], '', .08),
    ('户外运动', '金属', [350, 500, 750, 1000], 'ml', .2),
    ('户外运动', '合成聚合物', [100, 200, 400, 800], 'lm', .18),
    ('户外运动', '合成聚合物', [10, 5, 0, -5], '°C', .8),
    ('家居生活', '合成聚合物', [50, 70, 90, 100], '%', .6),
    ('家居生活', '合成聚合物', [10, 20, 30, 40], 'L', .3),
    ('家居生活', '陶瓷', [200, 300, 400, 500], 'ml', .25),
    ('厨房餐饮', '玻璃', [300, 600, 900, 1200], 'ml', .2),
    ('厨房餐饮', '金属', [400, 600, 800, 1000], 'ml', .3),
    ('厨房餐饮', '合成聚合物', [10, 20, 30, 40], '', .1),
    ('办公学习', '金属', [200, 400, 600, 800], 'lm', .4),
    ('办公学习', '金属', [2, 4, 6, 8], 'kg', .3),
    ('办公学习', '纸', [80, 120, 160, 200], '', .12),
    ('母婴宠物', '合成聚合物', [3, 5, 7, 9], 'kg', .7),
    ('母婴宠物', '合成聚合物', [1000, 1500, 2000, 2500], 'ml', .4),
    ('母婴宠物', '天然纤维', ['60×60', '70×70', '80×80', '90×90'], 'cm', .15),
    ('美妆个护', '合成聚合物', [30, 50, 80, 100], 'ml', .03),
    ('美妆个护', '合成聚合物', [800, 1000, 1400, 1800], 'W', .35),
    ('户外运动', '金属', [60, 80, 100, 120], 'kg', .9),
]
NAMES = {
'en': 'Hiking backpack|Packing cubes|Travel neck pillow|USB-C charger|Noise-cancelling headphones|Power bank|USB-C hub|Insulated bottle|Camping lantern|Sleeping bag|Blackout curtain|Storage box|Ceramic mug|Glass food container|Pour-over kettle|Food storage bags|Desk lamp|Laptop stand|Notebook|Pet carrier|Pet water fountain|Cotton baby bath towel|Travel toiletry bottle|Travel hair dryer|Folding camping stool',
'es': 'Mochila de senderismo|Organizadores de equipaje|Almohada cervical de viaje|Cargador USB-C|Auriculares con cancelación de ruido|Batería externa|Concentrador USB-C|Botella térmica|Linterna de camping|Saco de dormir|Cortina opaca|Caja de almacenamiento|Taza de cerámica|Recipiente de vidrio para alimentos|Hervidor de cuello de cisne|Bolsas para alimentos|Lámpara de escritorio|Soporte para portátil|Cuaderno|Transportín para mascotas|Fuente de agua para mascotas|Toalla de baño de algodón para bebé|Botella de aseo de viaje|Secador de viaje|Taburete plegable de camping',
'de': 'Wanderrucksack|Packwürfel|Reise-Nackenkissen|USB-C-Ladegerät|Kopfhörer mit Geräuschunterdrückung|Powerbank|USB-C-Hub|Isolierflasche|Campinglaterne|Schlafsack|Verdunkelungsvorhang|Aufbewahrungsbox|Keramiktasse|Glas-Frischhaltedose|Schwanenhalskessel|Frischhaltebeutel|Schreibtischlampe|Laptopständer|Notizbuch|Tiertransporttasche|Trinkbrunnen für Haustiere|Baumwoll-Badetuch für Babys|Reise-Kosmetikflasche|Reisehaartrockner|Faltbarer Campinghocker',
'fr': 'Sac à dos de randonnée|Organisateurs de valise|Coussin cervical de voyage|Chargeur USB-C|Casque à réduction de bruit|Batterie externe|Hub USB-C|Bouteille isotherme|Lanterne de camping|Sac de couchage|Rideau occultant|Boîte de rangement|Tasse en céramique|Boîte alimentaire en verre|Bouilloire à col de cygne|Sachets de conservation|Lampe de bureau|Support pour ordinateur portable|Carnet|Sac de transport pour animaux|Fontaine à eau pour animaux|Serviette de bain bébé en coton|Flacon de toilette de voyage|Sèche-cheveux de voyage|Tabouret de camping pliant',
'ja': '登山リュック|パッキングキューブ|旅行用ネックピロー|USB-C充電器|ノイズキャンセリングヘッドホン|モバイルバッテリー|USB-Cハブ|保温ボトル|キャンプランタン|寝袋|遮光カーテン|収納ボックス|陶器マグカップ|ガラス保存容器|細口ケトル|食品保存袋|デスクライト|ノートパソコンスタンド|ノート|ペットキャリーバッグ|ペット用給水器|綿のベビーバスタオル|旅行用詰め替えボトル|旅行用ドライヤー|折りたたみキャンプスツール',
'nl': 'Wandelrugzak|Inpakzakken|Reisnekkussen|USB-C-oplader|Koptelefoon met ruisonderdrukking|Powerbank|USB-C-hub|Thermosfles|Campinglantaarn|Slaapzak|Verduisteringsgordijn|Opbergdoos|Keramische mok|Glazen vershouddoos|Zwanenhalsketel|Bewaarzakken voor voedsel|Bureaulamp|Laptopstandaard|Notitieboek|Reistas voor huisdieren|Drinkfontein voor huisdieren|Katoenen babybadhanddoek|Reistoiletfles|Reisföhn|Opvouwbare campingkruk',
'pl': 'Plecak turystyczny|Organizery do walizki|Poduszka podróżna na szyję|Ładowarka USB-C|Słuchawki z redukcją hałasu|Powerbank|Koncentrator USB-C|Butelka termiczna|Latarnia kempingowa|Śpiwór|Zasłona zaciemniająca|Pojemnik do przechowywania|Kubek ceramiczny|Szklany pojemnik na żywność|Czajnik z długą wylewką|Woreczki na żywność|Lampka biurkowa|Podstawka pod laptop|Notes|Torba transportowa dla zwierząt|Fontanna dla zwierząt|Bawełniany ręcznik kąpielowy dla niemowląt|Podróżna butelka na kosmetyki|Suszarka podróżna|Składany stołek kempingowy',
'sv': 'Vandringsryggsäck|Packpåsar|Resenackkudde|USB-C-laddare|Hörlurar med brusreducering|Powerbank|USB-C-hubb|Termosflaska|Campinglykta|Sovsäck|Mörkläggningsgardin|Förvaringslåda|Keramikmugg|Matlåda av glas|Svanhalskittel|Förvaringspåsar för mat|Skrivbordslampa|Laptopställ|Anteckningsbok|Transportväska för husdjur|Vattenfontän för husdjur|Babybadhandduk i bomull|Reseflaska för hygienprodukter|Resehårtork|Hopfällbar campingpall',
'it': 'Zaino da escursionismo|Organizer per valigia|Cuscino cervicale da viaggio|Caricatore USB-C|Cuffie con cancellazione del rumore|Batteria esterna|Hub USB-C|Bottiglia termica|Lanterna da campeggio|Sacco a pelo|Tenda oscurante|Scatola portaoggetti|Tazza in ceramica|Contenitore alimentare in vetro|Bollitore a collo di cigno|Sacchetti per alimenti|Lampada da scrivania|Supporto per portatile|Quaderno|Trasportino per animali|Fontanella per animali|Asciugamano da bagno in cotone per neonati|Flacone da viaggio|Asciugacapelli da viaggio|Sgabello pieghevole da campeggio',
'pt': 'Mochila de caminhada|Organizadores de mala|Almofada cervical de viagem|Carregador USB-C|Auscultadores com cancelamento de ruído|Bateria externa|Hub USB-C|Garrafa térmica|Lanterna de campismo|Saco-cama|Cortina opaca|Caixa de arrumação|Caneca de cerâmica|Recipiente de vidro para alimentos|Chaleira de pescoço de cisne|Sacos para alimentos|Candeeiro de secretária|Suporte para portátil|Caderno|Transportadora para animais|Fonte de água para animais|Toalha de banho de algodão para bebé|Frasco de viagem|Secador de viagem|Banco dobrável de campismo',
}
PURPOSES = {
'en': 'Carry equipment on day hikes.|Separate clothing inside a suitcase.|Support the neck during a seated journey.|Charge USB-C devices; check the required wattage.|Listen during travel; compare the stated noise reduction.|Recharge a phone away from a socket.|Connect several wired accessories to a laptop.|Carry hot or cold drinks.|Light a tent after sunset.|Sleep at a campsite; compare comfort temperatures.|Reduce daylight in a bedroom.|Organize items on a shelf.|Serve coffee or tea.|Store a prepared meal.|Control the flow when brewing coffee.|Separate food portions in a refrigerator.|Illuminate a reading area.|Raise a laptop on a desk.|Write notes on paper.|Carry a small pet; check the maximum load.|Provide drinking water for a pet.|Dry a baby after bathing.|Pack small quantities of toiletries.|Dry hair while travelling.|Take a seat at a campsite; check the load limit.',
'es': 'Transporta equipo en excursiones.|Separa la ropa dentro de la maleta.|Apoya el cuello al viajar sentado.|Carga dispositivos USB-C; comprueba la potencia necesaria.|Escucha música durante el viaje; compara la reducción de ruido.|Recarga el teléfono lejos de un enchufe.|Conecta varios accesorios al portátil.|Transporta bebidas calientes o frías.|Ilumina la tienda de campaña.|Duerme en el camping; compara la temperatura de confort.|Reduce la luz en el dormitorio.|Ordena objetos en una estantería.|Sirve café o té.|Guarda una comida preparada.|Controla el flujo al preparar café.|Separa porciones en la nevera.|Ilumina una zona de lectura.|Eleva el portátil sobre la mesa.|Escribe notas en papel.|Transporta una mascota pequeña; comprueba la carga máxima.|Proporciona agua a una mascota.|Seca al bebé después del baño.|Lleva pequeñas cantidades de productos de aseo.|Seca el pelo durante el viaje.|Siéntate en el camping; comprueba el límite de carga.',
'de': 'Für Ausrüstung bei Tageswanderungen.|Trennt Kleidung im Koffer.|Stützt den Nacken auf Reisen im Sitzen.|Lädt USB-C-Geräte; benötigte Leistung prüfen.|Für Musik unterwegs; angegebene Geräuschreduktion vergleichen.|Lädt das Telefon ohne Steckdose.|Verbindet mehrere Zubehörgeräte mit dem Laptop.|Für heiße oder kalte Getränke.|Beleuchtet das Zelt nach Sonnenuntergang.|Für Übernachtungen beim Camping; Komforttemperatur vergleichen.|Reduziert Tageslicht im Schlafzimmer.|Ordnet Gegenstände im Regal.|Für Kaffee oder Tee.|Bewahrt eine vorbereitete Mahlzeit auf.|Für kontrolliertes Aufgießen von Kaffee.|Trennt Lebensmittelportionen im Kühlschrank.|Beleuchtet den Lesebereich.|Erhöht den Laptop auf dem Schreibtisch.|Für handschriftliche Notizen.|Transportiert kleine Haustiere; maximale Traglast beachten.|Stellt Trinkwasser für Haustiere bereit.|Trocknet das Baby nach dem Baden.|Für kleine Mengen an Pflegeprodukten.|Trocknet die Haare auf Reisen.|Sitzplatz beim Camping; Belastungsgrenze beachten.',
'fr': 'Transportez votre équipement en randonnée.|Séparez les vêtements dans la valise.|Soutenez la nuque pendant un trajet assis.|Chargez les appareils USB-C ; vérifiez la puissance requise.|Écoutez en voyage ; comparez la réduction du bruit annoncée.|Rechargez un téléphone sans prise à proximité.|Connectez plusieurs accessoires à un ordinateur portable.|Transportez des boissons chaudes ou froides.|Éclairez la tente après le coucher du soleil.|Dormez au camping ; comparez la température de confort.|Réduisez la lumière du jour dans la chambre.|Rangez les objets sur une étagère.|Servez du café ou du thé.|Conservez un repas préparé.|Contrôlez le débit pour préparer un café filtre.|Séparez les portions au réfrigérateur.|Éclairez un espace de lecture.|Surélevez un ordinateur sur le bureau.|Prenez des notes sur papier.|Transportez un petit animal ; vérifiez la charge maximale.|Mettez de l’eau à disposition de votre animal.|Séchez bébé après le bain.|Emportez de petites quantités de produits de toilette.|Séchez vos cheveux en voyage.|Asseyez-vous au camping ; vérifiez la charge maximale.',
'ja': '日帰り登山の装備を運びます。|スーツケースの中で衣類を分けます。|座って移動するときに首を支えます。|USB-C機器を充電します。必要な出力を確認してください。|移動中の音楽鑑賞に。記載の騒音低減量を比較してください。|コンセントのない場所でスマートフォンを充電します。|複数の周辺機器をパソコンにつなぎます。|温かい飲み物や冷たい飲み物を持ち運びます。|日没後のテントを照らします。|キャンプでの就寝に。快適温度を比較してください。|寝室に入る日光を減らします。|棚の小物を整理します。|コーヒーやお茶を入れます。|調理済みの食事を保存します。|ハンドドリップの注湯量を調整します。|冷蔵庫で食品を小分けにします。|読書スペースを照らします。|机の上でパソコンの位置を高くします。|紙にメモを書きます。|小型ペットを運びます。最大荷重を確認してください。|ペットの飲み水を用意します。|入浴後の赤ちゃんを拭きます。|少量の洗面用品を詰め替えます。|旅行先で髪を乾かします。|キャンプで座るために。耐荷重を確認してください。',
'nl': 'Draag uitrusting tijdens dagwandelingen.|Houd kleding gescheiden in een koffer.|Ondersteun de nek tijdens een zittende reis.|Laad USB-C-apparaten op; controleer het vereiste vermogen.|Luister onderweg; vergelijk de opgegeven ruisonderdrukking.|Laad een telefoon op zonder stopcontact.|Verbind meerdere accessoires met een laptop.|Neem warme of koude dranken mee.|Verlicht de tent na zonsondergang.|Slaap op de camping; vergelijk de comforttemperatuur.|Verminder daglicht in de slaapkamer.|Orden spullen op een plank.|Serveer koffie of thee.|Bewaar een bereide maaltijd.|Regel de waterstraal bij het koffiezetten.|Verdeel porties in de koelkast.|Verlicht een leesplek.|Zet een laptop hoger op het bureau.|Schrijf notities op papier.|Vervoer een klein huisdier; controleer het draagvermogen.|Bied drinkwater aan een huisdier.|Droog de baby af na het bad.|Neem kleine hoeveelheden toiletartikelen mee.|Droog het haar op reis.|Ga zitten op de camping; controleer de maximale belasting.',
'pl': 'Przenoś wyposażenie na jednodniowe wędrówki.|Oddzielaj ubrania w walizce.|Podpieraj szyję podczas podróży w pozycji siedzącej.|Ładuj urządzenia USB-C; sprawdź wymaganą moc.|Słuchaj w podróży; porównaj podaną redukcję hałasu.|Ładuj telefon bez dostępu do gniazdka.|Podłącz kilka akcesoriów do laptopa.|Zabierz ciepłe lub zimne napoje.|Oświetl namiot po zachodzie słońca.|Śpij na kempingu; porównaj temperaturę komfortu.|Ogranicz światło dzienne w sypialni.|Uporządkuj przedmioty na półce.|Podawaj kawę lub herbatę.|Przechowuj przygotowany posiłek.|Kontroluj strumień wody podczas parzenia kawy.|Oddzielaj porcje żywności w lodówce.|Oświetl miejsce do czytania.|Unieś laptop na biurku.|Zapisuj notatki na papierze.|Przewoź małe zwierzę; sprawdź maksymalne obciążenie.|Zapewnij zwierzęciu wodę do picia.|Osusz niemowlę po kąpieli.|Zabierz niewielkie ilości kosmetyków.|Susz włosy w podróży.|Usiądź na kempingu; sprawdź dopuszczalne obciążenie.',
'sv': 'Bär utrustning på dagsvandringar.|Separera kläder i resväskan.|Stöd nacken under en sittande resa.|Ladda USB-C-enheter; kontrollera nödvändig effekt.|Lyssna på resan; jämför angiven brusreducering.|Ladda telefonen utan vägguttag.|Anslut flera tillbehör till en bärbar dator.|Ta med varma eller kalla drycker.|Lys upp tältet efter solnedgången.|Sov på campingen; jämför komforttemperaturen.|Minska dagsljuset i sovrummet.|Ordna saker på en hylla.|Servera kaffe eller te.|Förvara en färdig måltid.|Kontrollera vattenflödet vid kaffebryggning.|Dela upp matportioner i kylskåpet.|Lys upp en läsplats.|Höj den bärbara datorn på skrivbordet.|Skriv anteckningar på papper.|Transportera ett litet husdjur; kontrollera maximal belastning.|Ge husdjuret tillgång till dricksvatten.|Torka barnet efter badet.|Ta med små mängder hygienprodukter.|Torka håret på resan.|Sitt på campingen; kontrollera viktgränsen.',
'it': 'Trasporta attrezzatura durante le escursioni.|Separa gli indumenti nella valigia.|Sostieni il collo durante un viaggio da seduto.|Ricarica dispositivi USB-C; controlla la potenza richiesta.|Ascolta in viaggio; confronta la riduzione del rumore dichiarata.|Ricarica il telefono lontano da una presa.|Collega più accessori al portatile.|Porta bevande calde o fredde.|Illumina la tenda dopo il tramonto.|Dormi in campeggio; confronta la temperatura di comfort.|Riduci la luce nella camera da letto.|Organizza gli oggetti su uno scaffale.|Servi caffè o tè.|Conserva un pasto pronto.|Controlla il flusso durante la preparazione del caffè.|Separa le porzioni nel frigorifero.|Illumina una zona di lettura.|Solleva il portatile sulla scrivania.|Scrivi appunti su carta.|Trasporta un piccolo animale; controlla il carico massimo.|Offri acqua da bere al tuo animale.|Asciuga il neonato dopo il bagno.|Porta piccole quantità di prodotti da bagno.|Asciuga i capelli in viaggio.|Siediti in campeggio; controlla il limite di carico.',
'pt': 'Leva equipamento em caminhadas de um dia.|Separa a roupa dentro da mala.|Apoia o pescoço durante uma viagem sentado.|Carrega dispositivos USB-C; verifica a potência necessária.|Ouve música em viagem; compara a redução de ruído indicada.|Carrega o telemóvel longe de uma tomada.|Liga vários acessórios ao portátil.|Transporta bebidas quentes ou frias.|Ilumina a tenda depois do pôr do sol.|Dorme no parque de campismo; compara a temperatura de conforto.|Reduz a luz no quarto.|Organiza objetos numa prateleira.|Serve café ou chá.|Guarda uma refeição preparada.|Controla o fluxo ao preparar café.|Separa porções no frigorífico.|Ilumina uma área de leitura.|Eleva o portátil na secretária.|Escreve notas em papel.|Transporta um animal pequeno; verifica a carga máxima.|Disponibiliza água para o animal beber.|Seca o bebé depois do banho.|Leva pequenas quantidades de produtos de higiene.|Seca o cabelo em viagem.|Senta-te no campismo; verifica o limite de carga.',
}
# 重复使用的属性字段按语种翻译，业务值来自 FAMILY_SPECS，不由翻译改写。
FEATURE_KEYS = ['capacity', 'count', 'weight', 'power', 'noise', 'capacity', 'ports', 'capacity', 'brightness', 'temperature', 'blocking', 'capacity', 'capacity', 'capacity', 'capacity', 'count', 'brightness', 'load', 'pages', 'load', 'capacity', 'size', 'capacity', 'power', 'load']
FIELD_ORDER = 'capacity count weight power noise ports brightness temperature blocking load pages size'.split()
FIELDS = {
'en': 'Capacity|Pieces|Weight|Power|Noise reduction|Ports|Brightness|Comfort temperature|Light blocking|Maximum load|Pages|Size',
'es': 'Capacidad|Piezas|Peso|Potencia|Reducción de ruido|Puertos|Luminosidad|Temperatura de confort|Bloqueo de luz|Carga máxima|Páginas|Tamaño',
'de': 'Kapazität|Stückzahl|Gewicht|Leistung|Geräuschreduktion|Anschlüsse|Helligkeit|Komforttemperatur|Lichtblockierung|Maximale Traglast|Seiten|Größe',
'fr': 'Capacité|Pièces|Poids|Puissance|Réduction du bruit|Ports|Luminosité|Température de confort|Occultation|Charge maximale|Pages|Dimensions',
'ja': '容量|個数|重量|出力|騒音低減量|ポート数|明るさ|快適温度|遮光率|最大荷重|ページ数|サイズ',
'nl': 'Inhoud|Aantal|Gewicht|Vermogen|Ruisonderdrukking|Poorten|Helderheid|Comforttemperatuur|Lichtblokkering|Maximale belasting|Pagina’s|Afmetingen',
'pl': 'Pojemność|Liczba sztuk|Masa|Moc|Redukcja hałasu|Porty|Jasność|Temperatura komfortu|Blokowanie światła|Maksymalne obciążenie|Strony|Wymiary',
'sv': 'Kapacitet|Antal|Vikt|Effekt|Brusreducering|Portar|Ljusstyrka|Komforttemperatur|Ljusblockering|Maximal belastning|Sidor|Storlek',
'it': 'Capacità|Pezzi|Peso|Potenza|Riduzione del rumore|Porte|Luminosità|Temperatura di comfort|Oscuramento|Carico massimo|Pagine|Dimensioni',
'pt': 'Capacidade|Peças|Peso|Potência|Redução de ruído|Portas|Luminosidade|Temperatura de conforto|Bloqueio de luz|Carga máxima|Páginas|Dimensões',
}
CATEGORY_ORDER = ['旅行装备', '数码配件', '家居生活', '户外运动', '美妆个护', '厨房餐饮', '办公学习', '母婴宠物']
CATEGORIES = {
'en': 'Travel|Electronics|Home|Outdoors|Personal care|Kitchen|Office|Baby and pets',
'es': 'Viaje|Electrónica|Hogar|Aire libre|Cuidado personal|Cocina|Oficina|Bebés y mascotas',
'de': 'Reise|Elektronik|Wohnen|Outdoor|Körperpflege|Küche|Büro|Baby und Haustiere',
'fr': 'Voyage|Électronique|Maison|Plein air|Soins personnels|Cuisine|Bureau|Bébé et animaux',
'ja': '旅行用品|電子機器|家庭用品|アウトドア|パーソナルケア|キッチン|文房具|ベビー・ペット用品',
'nl': 'Reizen|Elektronica|Wonen|Buitenleven|Persoonlijke verzorging|Keuken|Kantoor|Baby en huisdieren',
'pl': 'Podróże|Elektronika|Dom|Turystyka|Pielęgnacja|Kuchnia|Biuro|Dzieci i zwierzęta',
'sv': 'Resor|Elektronik|Hem|Friluftsliv|Personlig vård|Kök|Kontor|Baby och husdjur',
'it': 'Viaggio|Elettronica|Casa|Attività all’aperto|Cura personale|Cucina|Ufficio|Neonati e animali',
'pt': 'Viagem|Eletrónica|Casa|Ar livre|Cuidados pessoais|Cozinha|Escritório|Bebés e animais',
}
MATERIAL_ORDER = ['合成聚合物', '金属', '陶瓷', '玻璃', '纸', '天然纤维']
MATERIALS = {
'en': 'Synthetic polymer|Metal|Ceramic|Glass|Paper|Cotton',
'es': 'Polímero sintético|Metal|Cerámica|Vidrio|Papel|Algodón',
'de': 'Synthetisches Polymer|Metall|Keramik|Glas|Papier|Baumwolle',
'fr': 'Polymère synthétique|Métal|Céramique|Verre|Papier|Coton',
'ja': '合成ポリマー|金属|陶器|ガラス|紙|綿',
'nl': 'Synthetisch polymeer|Metaal|Keramiek|Glas|Papier|Katoen',
'pl': 'Polimer syntetyczny|Metal|Ceramika|Szkło|Papier|Bawełna',
'sv': 'Syntetisk polymer|Metall|Keramik|Glas|Papper|Bomull',
'it': 'Polimero sintetico|Metallo|Ceramica|Vetro|Carta|Cotone',
'pt': 'Polímero sintético|Metal|Cerâmica|Vidro|Papel|Algodão',
}
# 材质标签、颜色、型号标签同样属于本地化文本，不能留下中文或英文模板壳。
LABELS = {
'en': 'Material|Black|Grey|Model', 'es': 'Material|Negro|Gris|Modelo',
'de': 'Material|Schwarz|Grau|Modell', 'fr': 'Matériau|Noir|Gris|Modèle',
'ja': '材質|黒|灰色|型番', 'nl': 'Materiaal|Zwart|Grijs|Model',
'pl': 'Materiał|Czarny|Szary|Model', 'sv': 'Material|Svart|Grå|Modell',
'it': 'Materiale|Nero|Grigio|Modello', 'pt': 'Material|Preto|Cinzento|Modelo',
}


def localized_family(language: str, family: int) -> dict:
    category, material, values, unit, weight = FAMILY_SPECS[family]
    fields = dict(zip(FIELD_ORDER, FIELDS[language].split('|'), strict=True))
    labels = LABELS[language].split('|')
    return {
        'name': NAMES[language].split('|')[family],
        'purpose': PURPOSES[language].split('|')[family],
        'feature': fields[FEATURE_KEYS[family]],
        'category': dict(zip(CATEGORY_ORDER, CATEGORIES[language].split('|'), strict=True))[category],
        'material': dict(zip(MATERIAL_ORDER, MATERIALS[language].split('|'), strict=True))[material],
        'material_label': labels[0], 'colors': labels[1:3], 'model_label': labels[3],
    }
