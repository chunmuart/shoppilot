"""按文档覆盖主题，并核对静态知识是否能够回答确定事实请求。"""
from __future__ import annotations
import asyncio
import re
from agentscope.rag import KnowledgeBase


def unsupported_fact_reason(question: str) -> str | None:
    """只拦截明显越过证据边界的确定事实请求。"""
    if re.search(r"(如何|怎么).*(核对|查证|计算|判断)|需要哪些.*(资料|参数|证据)", question) and not re.search(r"直接(告诉|给出)", question):
        return None
    if re.search(r"明年.*(新品|售价)|明[日天].*(汇率|收盘价|价格)|预测.*(汇率|收盘价)", question):
        return "future_fact_not_available"
    if re.search(r"(没有|缺少|未提供|未给出?|没给|未确定|不知道).*(目的国|国家|地址|尺寸|容量|重量|价格|币种)", question) and re.search(r"(精确|准确|确定|能否|直接告诉|直接给出)", question):
        return "missing_required_parameters"
    if re.search(r"(没有|缺少|未经核实|未经证实).*来源|网传|传闻", question) and re.search(r"直接告诉|直接给出|确定|准确", question):
        return "unverified_source_claim"
    if re.search(r"(未登记|未知|不明).*(平台|品牌|商家).*(承诺|时效|价格|库存|保修|售后)", question):
        return "entity_outside_knowledge_scope"
    if re.search(r"(精确|准确|确定).*(税率|税额|关税|清关时效|配送时效|体积重|官方零售价)", question):
        return "requires_structured_or_live_evidence"
    if re.search(r"直接告诉.*(配送服务等级|履约承诺|实时库存)", question) or re.search(r"(某品牌|某商家|未知品牌).*(官方|价格|售价|发售)", question):
        return "entity_outside_knowledge_scope"
    return None


def _title_core(title: str) -> str:
    return re.sub(r"\s", "", re.sub(r"(评测知识快照|品类洞察|选购指南|知识快照)$", "", title).strip())


def targeted_documents(documents, question: str, limit: int) -> list:
    """使用已登记标题和地域定位明确主题；不读取评测 ID 或答案。"""
    query = re.sub(r"[\s，,。？?、]", "", question).casefold()
    matches = []
    for document in documents:
        core = _title_core(str(document.metadata.get("document_title", ""))).casefold()
        if core and core in query:
            matches.append((query.index(core), -len(core), document))
    # 同位置优先最具体标题，避免总览挤掉其专门子主题；不同位置保证多主题覆盖。
    matches.sort(key=lambda item: item[:2])
    covered_until, selected = -1, []
    for start, negative_length, document in matches:
        if start >= covered_until:
            selected.append(document)
            covered_until = start - negative_length
    if not selected and re.search(r"政策|规则|法规|来源|有效期", question):
        regions = {word.upper() for word in re.findall(r"(?<![A-Za-z])[A-Za-z]{2,6}(?![A-Za-z])", question)}
        scoped = [d for d in documents if d.metadata.get("topic") == "policy" and d.metadata.get("region") in regions]
        # 只指定地域时，优先通则，再交给向量相关性选择；具体电池/材质问题仍走正常召回。
        if scoped and not re.search(r"电池|材质|运费|体积重", question):
            selected = sorted(scoped, key=lambda d: (len(str(d.metadata.get("document_title", ""))), d.document_id))[:1]
    return selected[:limit]


async def search_knowledge(knowledge_base, question: str, top_k: int = 3):
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValueError("知识结果数须在1到10之间")
    if unsupported_fact_reason(question):
        return []
    results = await knowledge_base.search(queries=[question], top_k=min(80, top_k * 8))
    targets = []
    if isinstance(knowledge_base, KnowledgeBase):
        targets = targeted_documents(await knowledge_base.list_documents(), question, top_k)
        present = {item.document_id for item in results}
        async def scoped_search(document):
            # 构造共享存储的只读视图，保留原有租户过滤，不修改共享 KB 状态。
            filters = dict(knowledge_base.metadata_filter or {})
            source = document.metadata.get("source")
            if not source or ("source" in filters and filters["source"] != source):
                return []
            filters["source"] = source
            view = KnowledgeBase(name="scoped_insight", description="主题内证据检索", embedding_model=knowledge_base.embedding_model,
                                 vector_store=knowledge_base.vector_store, collection=knowledge_base.collection, metadata_filter=filters)
            return await view.search(queries=[question], top_k=1)
        additions = await asyncio.gather(*(scoped_search(doc) for doc in targets if doc.document_id not in present))
        results += [item for group in additions for item in group]
    target_order = {doc.document_id: index for index, doc in enumerate(targets)}
    documents = {}
    for position, item in enumerate(results):
        rank = (0, target_order[item.document_id], position) if item.document_id in target_order else (1, position, position)
        if item.document_id not in documents:
            documents[item.document_id] = (rank, item)
    # 每篇文档保留最高相关片段；原始分数用于可回答判断，不伪造分数。
    return [item for _, item in sorted(documents.values(), key=lambda pair: pair[0])[:top_k]]
