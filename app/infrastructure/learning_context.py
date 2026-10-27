"""当前运行的策略读取审计范围，不向提示词注入内容。"""
from contextlib import contextmanager
from contextvars import ContextVar

learning_run = ContextVar("learning_run", default=None)


@contextmanager
def learning_scope(journal, run_id, buyer_id, session_id):
    value = {"path": journal.path, "run_id": run_id, "buyer_id": buyer_id, "session_id": session_id} if journal.learning_enabled else None
    token = learning_run.set(value)
    try:
        yield
    finally:
        learning_run.reset(token)
