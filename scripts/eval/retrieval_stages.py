"""分开计算候选池覆盖与最终排序损失，禁止把 Top-K 分数当作并集覆盖率。"""
from __future__ import annotations

from statistics import mean


def candidate_metrics(result, products_by_id, relevant):
    trace = result['retrieval_stages']
    merged = trace['merged_candidates']
    fused = trace['fused_candidates']
    ranked = trace['ranked_candidates']
    if (len(merged) != len(set(merged)) or len(fused) != len(set(fused))
            or set(merged) != set(fused)):
        raise ValueError('融合排序丢失或重复候选，不能当作合法并集评测')

    def canonical(pid):
        product = products_by_id[pid]
        return product.canonical_product_id or product.product_id

    pool = {canonical(pid) for pid in merged}
    ranked_ids = [canonical(pid) for pid in ranked]
    if set(ranked_ids) != pool or len(ranked_ids) != len(set(ranked_ids)):
        raise ValueError('排序后的同款去重改变了候选覆盖范围')
    final_product_ids = [hit['product_id'] for hit in result['hits']]
    if final_product_ids != ranked[:len(final_product_ids)]:
        raise ValueError('最终返回与实际排序名单前缀不一致')
    final = {canonical(pid) for pid in final_product_ids}
    gold = set(relevant)
    found = pool & gold
    lost = found - final
    return {
        'candidate_listing_count': len(merged),
        'candidate_canonical_count': len(pool),
        'candidate_canonical_ids': sorted(pool),
        'candidate_found_ids': sorted(found),
        'candidate_missing_ids': sorted(gold - pool),
        'candidate_recall': len(found) / len(gold) if gold else None,
        'candidate_empty_ok': not pool if not gold else None,
        'ranking_lost_ids': sorted(lost),
        'ranking_loss_recall': len(lost) / len(gold) if gold else None,
        'relevant_ranks': [{'canonical_id': pid, 'rank': i + 1}
                           for i, pid in enumerate(ranked_ids) if pid in gold],
    }


def aggregate_candidates(rows):
    recalls = [r['candidate_recall'] for r in rows if r['candidate_recall'] is not None]
    losses = [r['ranking_loss_recall'] for r in rows if r['ranking_loss_recall'] is not None]
    return {
        'cases': len(rows),
        'positive_cases': len(recalls),
        'recall': mean(recalls) if recalls else None,
        'mean_listing_count': mean(r['candidate_listing_count'] for r in rows),
        'mean_canonical_count': mean(r['candidate_canonical_count'] for r in rows),
        'ranking_loss_recall': mean(losses) if losses else None,
        'cases_with_ranking_loss': sum(bool(r['ranking_lost_ids']) for r in rows),
        'empty_failures': sum(r['candidate_empty_ok'] is False for r in rows),
    }


def verify_merge_union(rows):
    """相同查询、深度和过滤下，混合必须保留两条独立单路的实际候选。"""
    variants = {row['variant']: row for row in rows}
    if len(rows) != 3 or set(variants) != {'bm25', 'dense', 'hybrid'}:
        raise ValueError('缺少三路候选，无法验证合并覆盖')
    ids = {name: set(row['retrieval_stages']['merged_candidates'])
           for name, row in variants.items()}
    if ids['hybrid'] != ids['bm25'] | ids['dense']:
        raise ValueError('混合候选不是同一查询两条单路候选的完整并集')
    return {
        'case_id': rows[0]['case_id'],
        'bm25_count': len(ids['bm25']),
        'dense_count': len(ids['dense']),
        'merged_count': len(ids['hybrid']),
        'union_verified': True,
    }
