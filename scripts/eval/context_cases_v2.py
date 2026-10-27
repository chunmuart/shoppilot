"""第二轮独立参数集：改变商品身份、SKU价差、库存、费用、目的地和对话边界。

保留第一轮为回归数据；本文件在新模型运行前冻结。模板相同而具体数据未用于调参，
因此只称参数留出，不声称人工编写的全新任务分布。
"""
import copy
import json
import random
import re
from pathlib import Path
from scripts.eval.context_cases import build_cases


def build(seed=2026091017, prefix="v2", id_base=8101):
    root=Path(__file__).resolve().parents[2]
    catalog=[json.loads(x) for x in (root/'data/catalog-v1.jsonl').read_text().splitlines()][:5]
    rng=random.Random(seed)
    cases=[]
    for index, base in enumerate(build_cases()):
        case=copy.deepcopy(base)
        case['template_id']=base['id'];case['id']=prefix+'-'+base['id']
        products=copy.deepcopy(catalog)
        mapping={f'P{1001+j}':f'P{id_base+index*10+j}' for j in range(5)}
        def remap(text):
            return re.sub(r'P100[1-5]',lambda m:mapping[m.group()],text)
        products=json.loads(remap(json.dumps(products,ensure_ascii=False)))
        historical=rng.randrange(141,214,2);present=historical+rng.choice([-26,-18,22,34])
        black=rng.randrange(31,54);blue=rng.randrange(61,88);stock=rng.randrange(3,17)
        other=rng.randrange(231,284);shipping=rng.randrange(21,29);tax=rng.randrange(2,9)
        budget=rng.randrange(310,370);initial_budget=budget+150
        products[2]['skus'][0].update(price_major=historical,stock=black)
        products[2]['skus'][1].update(price_major=historical+17,stock=blue)
        products[0]['skus'][1]['price_major']=other
        dest=rng.choice(['US','DE','GB']);country={'US':'美国','DE':'德国','GB':'英国'}[dest]
        selected=products[2]['skus'][0]['sku_id'];excluded=products[1]['product_id']
        values={'129':str(historical),'139':str(present),'80':str(black),'60':str(blue),'7':str(stock),
                '199':str(other),'154':str(historical+shipping+tax),'25':str(shipping),'0':str(tax),
                '180':str(budget),'300':str(initial_budget),'JP':dest}
        def numeric(text):
            # 不替换商品ID内的数字，只替换事实或题面中的完整数字。
            text=remap(text)
            return re.sub(r'(?<![A-Za-z0-9])(?:'+ '|'.join(values) +r')(?![A-Za-z0-9])',lambda m:values[m.group()],text)
        case['question']=numeric(case['question'])
        case['contains']=[values.get(x,remap(x)) for x in case['contains']]
        case['fixture']={'catalog':products,'selected_sku':selected,'excluded_product':excluded,
            'current':{'product_id':products[2]['product_id'],'sku_id':selected,'price_major':present,'currency':'CNY','stock':stock,
                       'order_id':'ORD-EVAL','order_status':'CANCELLED','preferences':[],
                       'notice':'长期塑料排除偏好已删除，不再要求排除塑料。'},
            'initial':f'预算{initial_budget}元，寄到中国。',
            'change':f'预算改为{budget}元，寄到{country}。选中{selected}，不要下单。{excluded}太贵，淘汰。',
            'destination':dest,'shipping':shipping,'shipping_delta':rng.randrange(9,18),'tax':tax,
            'rounds':24,'change_round':rng.choice([4,6,7]),'compact_rounds':[9,18],'restart_round':14,
            'description_repetitions':rng.choice([2,4,8])}
        case['rounds']=24 if case['mode']=='long' else 0
        rules={
            'hold-old-price':[(r'历史|第一批|第\s*1\s*批',str(historical),str(present)),(r'当前|现价|现在',str(present),str(historical))],
            'hold-pair':[(remap('P1001-S2'),str(other),str(historical)),(selected,str(historical),str(other))],
            'hold-sku-stock':[(r'石墨黑|'+selected,str(black),str(blue)),(r'雾霾蓝|'+remap('P1003-S2'),str(blue),str(black))],
            'hold-blue':[(r'雾霾蓝|'+remap('P1003-S2'),str(blue),str(black))],
            'hold-tax':[(r'运费|配送费',str(shipping),str(tax)),(r'税费|税额',str(tax),str(shipping))],
        }
        if base['id'] in rules:case['association_rules']=rules[base['id']]
        case['critical']=base['id'] in {'dev-sku','dev-cost','dev-stock','dev-budget','dev-country'}
        cases.append(case)
    return cases


if __name__=='__main__':
    Path('eval/context/cases-v2.json').write_text(json.dumps(build(),ensure_ascii=False,indent=2)+'\n')
