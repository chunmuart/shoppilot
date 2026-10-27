"""模型目录和连接管理；客户端提交选择，运行快照只由服务端生成。"""
from __future__ import annotations
from urllib.parse import urlsplit
from typing import Literal
from fastapi import HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.presentation.identity import require_buyer, require_session, session_error
from app.domain.session.ports.session_store import SessionStoreError
from app.infrastructure.model_registry import ModelConfigurationError, ModelRevisionConflict


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    buyer_id: str
    profile_id: str = Field(min_length=1, max_length=128)
    profile_revision: int | None = Field(default=None, ge=1)
    window_id: str | None = None
    thinking: str | None = None
    expected_revision: int = Field(ge=0)



class ModelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class PreferencesRequest(ModelRequest):
    buyer_id: str
    visible: bool | None = None
    default_window: str | None = None
    thinking: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class DefaultRequest(ModelRequest):
    buyer_id: str
    profile_id: str = Field(min_length=1, max_length=128)
    window_id: str | None = None
    thinking: str | None = None
    expected_revision: int = Field(ge=0)


class CapabilitiesRequest(ModelRequest):
    context_max: int
    max_output: int = 8192
    max_input: int | None = None
    streaming: bool = True
    tool_calling: bool = True


class WindowRequest(ModelRequest):
    id: str
    tokens: int
    kind: Literal['local_budget'] = 'local_budget'


class ReasoningRequest(ModelRequest):
    kind: Literal['unsupported', 'boolean', 'effort']
    options: list[str]
    default: str


class CounterRequest(ModelRequest):
    kind: Literal['calibrated_estimate'] = 'calibrated_estimate'
    tokenizer_ref: None = None


class ConnectionRequest(ModelRequest):
    buyer_id: str
    preset_id: str | None = Field(default=None, min_length=1, max_length=128)
    credential_profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    credential_profile_revision: int | None = Field(default=None, ge=1)
    profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)
    provider: str = Field(default='自定义网关', max_length=200)
    protocol: Literal['openai_chat'] = 'openai_chat'
    model_id: str = Field(min_length=1, max_length=200)
    label: str = Field(default='', max_length=200)
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: str = Field(default='', repr=False, max_length=16384)
    capabilities: CapabilitiesRequest | None = None
    context_max: int = 128000
    max_output_tokens: int = 8192
    max_input_tokens: int | None = None
    window_options: list[WindowRequest] | None = Field(default=None, max_length=12)
    default_window: str | None = None
    reasoning: ReasoningRequest | None = None
    counter: CounterRequest | None = None
    visible: bool = True


class SaveConnectionRequest(ConnectionRequest):
    validation_ticket: str = Field(min_length=1, max_length=100)
    config_hash: str = Field(min_length=1, max_length=100)


def parsed_body(schema, body):
    try:
        return schema.model_validate(body).model_dump(exclude_none=True)
    except ValidationError:
        # 默认的字段校验错误会回显 input；连接字段可能包含 API Key。
        raise HTTPException(422, '模型配置字段、类型或取值无效，请检查后重试') from None


def management_allowed(request, buyer, settings):
    origin = request.headers.get('origin')
    origins = {value.rstrip('/') for value in settings.cors_origins}
    if settings.identity_mode == 'demo':
        origins |= {'http://localhost:5173', 'http://localhost:5174',
                    'http://127.0.0.1:5173', 'http://127.0.0.1:5174'}
        host = request.url.hostname
        # Host 来自请求，不能用它为自己建立 Origin 信任。只接受真实本地访问。
        local_host = host in {'localhost', '127.0.0.1', '::1'}
        local_peer = request.client and request.client.host in {'127.0.0.1', '::1', 'localhost', 'testclient'}
        if local_host:
            origins.add(str(request.base_url).rstrip('/'))
        return bool(local_host and local_peer and (not origin or origin.rstrip('/') in origins))
    return buyer in settings.model_admin_buyers and (not origin or origin.rstrip('/') in origins)


def register_model_routes(api, get_container):
    def service():
        container = get_container()
        if container.model_service is None:
            raise HTTPException(503, '模型选择需要数据库会话存储')
        return container.model_service

    async def administrator(request, body):
        buyer = await require_buyer(request, body.get('buyer_id', ''))
        if not management_allowed(request, buyer, get_container().settings):
            raise HTTPException(403, '模型连接管理只允许本机演示或授权管理员访问')
        return buyer

    def error(error):
        if isinstance(error, SessionStoreError):
            return session_error(error)
        return HTTPException(409 if isinstance(error, ModelRevisionConflict) else 422, str(error))

    @api.get('/commerce/models')
    async def catalog(request: Request, buyer_id: str = Query(min_length=1), session_id: str = Query(min_length=1)):
        buyer = await require_buyer(request, buyer_id)
        await require_session(request, buyer, session_id, create=True)
        try:
            result = await service().view(session_id, buyer)
            return {**result, 'management_allowed': management_allowed(request, buyer, get_container().settings)}
        except (SessionStoreError, ModelConfigurationError) as exc:
            raise error(exc) from exc

    @api.patch('/commerce/sessions/{session_id}/model')
    async def select(session_id: str, body: SelectionRequest, request: Request):
        buyer = await require_buyer(request, body.buyer_id)
        await require_session(request, buyer, session_id, create=True)
        try:
            return {**await service().select(session_id, buyer, body.model_dump()),
                    'management_allowed': management_allowed(request, buyer, get_container().settings)}
        except (SessionStoreError, ModelConfigurationError) as exc:
            raise error(exc) from exc

    @api.patch('/commerce/models/{profile_id}/preferences')
    async def preferences(profile_id: str, body: dict, request: Request):
        buyer = await require_buyer(request, body.get('buyer_id', ''))
        body = parsed_body(PreferencesRequest, body)
        try:
            return await service().registry.set_preferences(profile_id, buyer,
                **{k: v for k, v in body.items() if k != 'buyer_id'})
        except ModelConfigurationError as exc:
            raise error(exc) from exc

    @api.post('/commerce/models/default')
    async def default_model(body: dict, request: Request):
        body = parsed_body(DefaultRequest, body)
        buyer = await require_buyer(request, body.get('buyer_id', ''))
        try:
            return await service().registry.set_default(buyer, body.get('profile_id', ''),
                body.get('window_id'), body.get('thinking'), body.get('expected_revision', 0))
        except ModelConfigurationError as exc:
            raise error(exc) from exc

    @api.post('/commerce/model-connections/validate')
    async def validate(body: dict, request: Request):
        buyer = await administrator(request, body)
        body = parsed_body(ConnectionRequest, body)
        try:
            return await service().registry.validate_connection(body, buyer,
                profile_id=body.get('profile_id'), expected_revision=body.get('expected_revision'))
        except ModelConfigurationError as exc:
            raise error(exc) from exc

    @api.post('/commerce/model-connections')
    async def save(body: dict, request: Request):
        buyer = await administrator(request, body)
        body = parsed_body(SaveConnectionRequest, body)
        try:
            return await service().registry.save_connection(body, buyer,
                validation_ticket=body.get('validation_ticket', ''), config_hash=body.get('config_hash', ''),
                profile_id=body.get('profile_id'), expected_revision=body.get('expected_revision'))
        except ModelConfigurationError as exc:
            raise error(exc) from exc
