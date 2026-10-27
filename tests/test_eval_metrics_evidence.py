from scripts.eval.metrics_evidence import collect_case_metrics, release_metrics


def test_no_service_usage_does_not_become_zero_cost_or_publishable_metrics():
    metrics = collect_case_metrics([], expected_turns=2, elapsed_ms=100)
    assert metrics['input_tokens'] is None and metrics['cost_usd'] is None
    result = release_metrics([{'metrics': metrics, 'verdict': 'PASS', 'p0_pass': True}])
    assert result['task_success_rate'] == 1
    assert result['latency_p95_ms'] == 100
    assert result['input_tokens'] is None


def test_every_dialogue_turn_requires_real_complete_usage():
    event = {'type': 'usage.summary', 'payload': {'usage_complete': True, 'input_tokens': 10, 'output_tokens': 4, 'cost_usd': None}}
    assert collect_case_metrics([event], expected_turns=2, elapsed_ms=100)['input_tokens'] is None
    metrics = collect_case_metrics([event, event], expected_turns=2, elapsed_ms=100)
    assert metrics['input_tokens'] == 20 and metrics['cost_usd'] is None
    result = release_metrics([{'metrics': metrics, 'verdict': 'FAIL', 'p0_pass': True}])
    assert result['input_tokens'] == 20 and result['task_success_rate'] == 0
