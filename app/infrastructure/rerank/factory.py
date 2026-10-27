"""运行链路与评测只请求专用 reranker，禁止回退到聊天模型。"""


def create_reranker(settings, *, throttle=None, bus=None):
    if settings.reranker_mode == "disabled":
        return None
    if settings.reranker_mode != "http":
        raise ValueError("RERANKER_MODE 只支持 http / disabled")
    from .http_reranker import HttpReranker

    return HttpReranker(settings) if settings.reranker_base_url else None
