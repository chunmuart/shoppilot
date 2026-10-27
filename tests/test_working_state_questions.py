"""回顾或核对问题不能覆盖已经明确的选购约束。"""
import copy
from types import SimpleNamespace
import pytest
from agentscope.state import AgentState
from agentscope.message import UserMsg
from app.infrastructure.context import ShoppingContext,ShoppingContextSnapshot
from app.infrastructure.context_governance import update_working_state,governance

@pytest.mark.parametrize('question',[
    '我选中的SKU、当前本次预算、收货国家、容量和材质排除是什么？只回顾需求',
    '颜色是什么？数量多少？是否需要真皮？',
    '预算300元吗？寄到中国吗？选中P1003-S1吗？',
    '请回顾用途、容量、颜色和重量要求。',
    'P1003-S1是35升、黑色、真皮的吗？',
])
def test_questions_preserve_constraints_and_selection(question):
    agent=SimpleNamespace(state=AgentState())
    token=ShoppingContext.set(ShoppingContextSnapshot('s','buyer','zh-CN','CNY'))
    try:
        update_working_state(agent,[UserMsg(name='buyer',content='预算180元，寄到日本。容量35升，颜色蓝色，数量2件，用途通勤，要求轻便，排除真皮。选中P1003-S2。')])
        before=copy.deepcopy(governance(agent)['working'])
        update_working_state(agent,[UserMsg(name='buyer',content=question)])
        after=governance(agent)['working']
        assert after['constraints']==before['constraints']
        assert after['selected']==before['selected']
        assert after['excluded']==before['excluded']
        assert after['latest_request']==question
    finally:ShoppingContext.reset(token)

def test_explicit_change_in_same_message_as_question_is_applied():
    agent=SimpleNamespace(state=AgentState())
    token=ShoppingContext.set(ShoppingContextSnapshot('s','buyer','zh-CN','CNY'))
    try:
        update_working_state(agent,[UserMsg(name='buyer',content='容量35升，颜色蓝色。预算180元。')])
        update_working_state(agent,[UserMsg(name='buyer',content='原来容量是多少？容量改为40升，颜色改为黑色。预算改为200元，寄到中国。')])
        constraints=governance(agent)['working']['constraints']
        assert constraints['capacity']['source']=='容量改为40升'
        assert constraints['color']['source']=='颜色改为黑色'
        assert constraints['budget']['value']=='200'
        assert constraints['destination']['value']=='CN'
    finally:ShoppingContext.reset(token)
