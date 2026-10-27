"""上下文评测的调用级计量，不采集提示词正文。"""
from contextvars import ContextVar
from opentelemetry import trace

context_call_kind = ContextVar('context_call_kind', default='business')
context_usage_sink = ContextVar('context_usage_sink', default=None)
context_diagnostic_sink = ContextVar('context_diagnostic_sink', default=None)
context_request_details = ContextVar('context_request_details', default=None)
evaluation_evidence_sink = ContextVar('evaluation_evidence_sink', default=None)


def record_evaluation_evidence(kind, payload):
    """仅隔离评测显式安装 sink；业务默认不采集原文，也不发送到 OTLP。"""
    sink = evaluation_evidence_sink.get()
    if sink is not None:
        from copy import deepcopy
        sink({'kind': kind, 'call_kind': context_call_kind.get(), 'payload': deepcopy(payload)})


def record_context_diagnostic(event):
    """调用链诊断只传递结构化计数/状态；默认不持久化买家输入或工具正文。"""
    sink = context_diagnostic_sink.get()
    if sink is not None:
        sink(event)


def record_context_usage(input_tokens, output_tokens, elapsed_ms, *, cache=None, start_time=None, ttft_ms=None, model_identity=None):
    sample = {'kind':context_call_kind.get(), 'input_tokens':input_tokens, 'output_tokens':output_tokens, 'elapsed_ms':elapsed_ms}
    if context_request_details.get() is not None:
        sample['request_context'] = dict(context_request_details.get())
    if cache is not None:sample['prompt_cache']=cache
    if model_identity is not None:sample['model_execution']=dict(model_identity)
    # 非流式不能把完整响应耗时伪装成首字时间。
    sample['ttft_ms'] = ttft_ms
    sink=context_usage_sink.get()
    if sink is not None:sink(sample)
    # Langfuse/OTel 只接收数值；未知值明确记录为未知。
    span=trace.get_current_span()
    span.set_attribute('shoppilot.context.call_kind',sample['kind'])
    span.set_attribute('shoppilot.context.usage_known',input_tokens is not None)
    if input_tokens is not None:span.set_attribute('shoppilot.context.input_tokens',input_tokens)
    if output_tokens is not None:span.set_attribute('shoppilot.context.output_tokens',output_tokens)
    if cache is not None:
        # 独立 attempt span 保留逐次摘要/回退消耗，不向 SDK generation 重复添加计费字段。
        with trace.get_tracer('shoppilot.prompt_cache').start_as_current_span(
                'shoppilot.model.attempt', start_time=start_time) as attempt:
            attempt.set_attribute('langfuse.observation.type', 'span')
            attempt.set_attribute('shoppilot.context.call_kind', sample['kind'])
            attempt.set_attribute('shoppilot.context.elapsed_ms', elapsed_ms)
            attempt.set_attribute('shoppilot.context.ttft_ms_known', ttft_ms is not None)
            if model_identity:
                for key in ('identity', 'requested_model', 'profile_id', 'profile_revision', 'response_model_matches'):
                    if model_identity.get(key) is not None:
                        attempt.set_attribute('shoppilot.model.' + key, model_identity[key])
            if ttft_ms is not None:attempt.set_attribute('shoppilot.context.ttft_ms', ttft_ms)
            for key, val in [('input_tokens', input_tokens), ('output_tokens', output_tokens)]:
                attempt.set_attribute('shoppilot.context.'+key+'_known', val is not None)
                if val is not None:attempt.set_attribute('shoppilot.context.'+key, val)
            for key, val in cache.items():
                if val is not None:attempt.set_attribute('shoppilot.prompt_cache.'+key, val)
            for key in ('cache_read_tokens','cache_write_tokens','reported_cost'):
                attempt.set_attribute('shoppilot.prompt_cache.'+key+'_known', cache.get(key) is not None)
