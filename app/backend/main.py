from __future__ import annotations

import asyncio
import hmac
import httpx
import json
import os
import queue
import re
from contextlib import asynccontextmanager
from enum import IntEnum
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .errors import (
    DuplicateResourceError,
    InsufficientEmailsError,
    MongoUnavailableError,
    ProxyCountryUnavailableError,
    ResourceNotFoundError,
    RoxyWorkspaceMissingError,
    RunConflictError,
    RunNotFoundError,
)
from .mongo_manager import MongoManager
from .payment_tools import (
    AccessTokenExtractInput,
    AccessTokenExtractResult,
    extract_access_tokens,
)
from .oai_payment_extractor.web.env import load_configured_env
from .payment_extractor_service import (
    PaymentExtractorBulkDelete,
    PaymentExtractorConcurrencyUpdate,
    PaymentExtractorProxyTest,
    PaymentExtractorProxySource,
    PaymentExtractorService,
    PaymentExtractorServiceError,
    PaymentExtractorTaskCreate,
    PaymentExtractorTaskRetry,
)
from .paypal_agreement_service import (
    PaypalAgreementService,
    PaypalAgreementServiceError,
)
from .pipeline_service import (
    AccountPipelineService,
    HeroSmsSettingsUpdate,
    PipelineBatchInput,
    PipelineOtpInput,
    PipelinePaidExportInput,
    PipelinePaidExportStatusInput,
    PipelinePaidMailCheckInput,
    PipelinePaymentInput,
    PipelineServiceError,
    PipelineSettingsUpdate,
    SmsReceiverBatchInput,
    SmsReceiverSettingsUpdate,
)
from .probe_store import MongoProbeStore
from .plan_check_service import AccountPlanCheckService
from .resource_models import (
    AccountCreate,
    AccountExportInput,
    AccountRecord,
    AccountPlanCheckInput,
    AccountPlanCheckResult,
    BulkIdsInput,
    BrowserProbeRunCreate,
    DeleteResult,
    EmailExportInput,
    EmailRecord,
    HealthResponse,
    ImportResult,
    MockRunCreate,
    OverviewStats,
    Page,
    ProxyRecord,
    ProxyCountrySummary,
    ProxyGroupSummary,
    ProxyGroupUpdate,
    ProxyImportInput,
    ProxyUpdate,
    RawImportInput,
    RunState,
    TextExport,
    WorkerSnapshot,
)
from .resource_service import MongoResourceStore, ResourceService
from .run_log_store import CorruptRunLogError, RunLogFile, RunLogNotFoundError, RunLogStore, RunLogSummary
from .run_manager import RunManager
from .roxy_client import RoxyApiError
from .run_store import MongoRunStore, MongoRunWorkerStore
from .settings_store import (
    CorruptSettingsError,
    ExecutionSettings,
    ExecutionSettingsInput,
    SettingsStore,
)

PageNumber = Annotated[int, Query(ge=1)]
SearchQuery = Annotated[str, Query(max_length=320)]


class PageSizeOption(IntEnum):
    TEN = 10
    TWENTY = 20
    FIFTY = 50
    ONE_HUNDRED = 100


def _snake_key(value: str) -> str:
    result = str(value)
    result = result.replace("-", "_")
    result = re.sub(r"(?<!^)(?=[A-Z])", "_", result)
    return result.lower()


def _snakeize(value):
    if isinstance(value, dict):
        return {_snake_key(str(key)): _snakeize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snakeize(item) for item in value]
    return value


def _task_response(value: dict, *, snake: bool = False) -> dict:
    result = _snakeize(value) if snake else dict(value)
    task_id = str(value.get("task_id") or value.get("taskId") or "")
    if task_id:
        if snake:
            result["status_url"] = f"/api/tasks/{task_id}"
            result["websocket_url"] = "/ws/tasks"
        else:
            result["statusUrl"] = f"/api/payment-extractor/tasks/{task_id}"
            result["websocketUrl"] = "/ws/tasks"
    return result


def _web_password() -> str:
    return os.getenv("OPLL_WEB_PASSWORD", "")


def _password_matches(request: Request, password: str) -> bool:
    if not password:
        return True
    supplied = request.headers.get("X-Workbench-Password", "")
    return hmac.compare_digest(supplied, password)


def _paypal_agreement_error(exc: PaypalAgreementServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _rewrite_paypal_agreement_csp(value: str) -> str:
    if not value:
        return "frame-ancestors 'self'"
    rewritten = re.sub(r"frame-ancestors\s+[^;]+", "frame-ancestors 'self'", value, flags=re.IGNORECASE)
    if rewritten == value and "frame-ancestors" not in rewritten.lower():
        rewritten = f"{rewritten.rstrip(';')}; frame-ancestors 'self'"
    return rewritten


def create_app(
    settings_path: Path | None = None,
    log_dir: Path | None = None,
    mongo_manager: MongoManager | None = None,
    payment_extractor_service: PaymentExtractorService | None = None,
    paypal_agreement_service: PaypalAgreementService | None = None,
    pipeline_service: AccountPipelineService | None = None,
) -> FastAPI:
    load_configured_env()
    settings_store = SettingsStore(settings_path) if settings_path else SettingsStore()
    run_log_store = RunLogStore(log_dir) if log_dir else RunLogStore()
    mongo = mongo_manager or MongoManager()
    resource_store = MongoResourceStore(mongo)
    resource_service = ResourceService(resource_store)
    probe_store = MongoProbeStore(mongo)
    plan_check_service = AccountPlanCheckService(resource_store, probe_store)
    extractor_service = payment_extractor_service or PaymentExtractorService()
    agreement_service = paypal_agreement_service or PaypalAgreementService()
    account_pipeline = pipeline_service or AccountPipelineService(
        resource_store,
        extractor_service,
        agreement_service,
    )
    run_store = MongoRunStore(mongo)
    worker_store = MongoRunWorkerStore(mongo)
    run_manager = RunManager(
        mongo,
        resource_store,
        run_store,
        run_log_store,
        settings_store,
        probe_store=probe_store,
        worker_store=worker_store,
    )
    mongo.add_reconnect_callback(run_manager.recover)
    mongo.add_reconnect_callback(probe_store.ensure_indexes)
    mongo.add_reconnect_callback(account_pipeline.ensure_indexes)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        run_log_store.prune_terminal_runs()
        await mongo.start()
        await account_pipeline.start()
        try:
            yield
        finally:
            await run_manager.shutdown()
            await account_pipeline.stop()
            extractor_service.close()
            agreement_service.close()
            await mongo.stop()

    app = FastAPI(
        title="AutoRegister Local Control Service",
        version="0.4.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def payment_workbench_auth(request: Request, call_next):
        # Keep the local service open by default, while matching the reference
        # workbench's header gate whenever OPLL_WEB_PASSWORD is configured.
        # Registration/resource APIs are deliberately outside this namespace.
        path = request.url.path
        protected = (
            path == "/api/defaults"
            or path.startswith("/api/tasks")
            or path in {"/api/proxy/source", "/api/proxy/test"}
            or path.startswith("/api/payment-extractor")
            or path.startswith("/api/pipeline")
        )
        if protected and not _password_matches(request, _web_password()):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"ok": False, "error": "unauthorized"},
            )
        return await call_next(request)

    app.state.settings_store = settings_store
    app.state.run_log_store = run_log_store
    app.state.mongo_manager = mongo
    app.state.resource_store = resource_store
    app.state.resource_service = resource_service
    app.state.probe_store = probe_store
    app.state.plan_check_service = plan_check_service
    app.state.payment_extractor_service = extractor_service
    app.state.paypal_agreement_service = agreement_service
    app.state.account_pipeline = account_pipeline
    app.state.run_store = run_store
    app.state.worker_store = worker_store
    app.state.run_manager = run_manager

    @app.exception_handler(MongoUnavailableError)
    async def mongodb_unavailable_handler(
        _request: Request, exc: MongoUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": {
                    "code": "mongodb_unavailable",
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(DuplicateResourceError)
    async def duplicate_handler(
        _request: Request, exc: DuplicateResourceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": {"code": "duplicate_resource", "message": str(exc)}},
        )

    @app.exception_handler(ResourceNotFoundError)
    async def missing_resource_handler(
        _request: Request, exc: ResourceNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": {"code": "resource_not_found", "message": str(exc)}},
        )

    @app.exception_handler(PipelineServiceError)
    async def pipeline_error_handler(
        _request: Request, exc: PipelineServiceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        mongo_health = mongo.health()
        return HealthResponse(
            status="ok" if mongo_health.status == "online" else "degraded",
            mongodb=mongo_health,
        )

    @app.get("/api/paypal-agreement/status")
    async def paypal_agreement_status(request: Request) -> dict:
        return await asyncio.to_thread(request.app.state.paypal_agreement_service.status)

    @app.post("/api/paypal-agreement/start")
    async def paypal_agreement_start(request: Request) -> dict:
        try:
            return await asyncio.to_thread(request.app.state.paypal_agreement_service.start)
        except PaypalAgreementServiceError as exc:
            raise _paypal_agreement_error(exc) from exc

    @app.post("/api/paypal-agreement/stop")
    async def paypal_agreement_stop(request: Request) -> dict:
        return await asyncio.to_thread(request.app.state.paypal_agreement_service.stop)

    @app.get("/api/pipeline/settings")
    async def pipeline_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.settings()

    @app.put("/api/pipeline/settings")
    async def update_pipeline_settings(
        payload: PipelineSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_settings(payload)

    @app.get("/api/herosms/settings")
    async def hero_sms_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.hero_sms_settings()

    @app.put("/api/herosms/settings")
    async def update_hero_sms_settings(
        payload: HeroSmsSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_hero_sms_settings(payload)

    @app.get("/api/herosms/countries")
    async def hero_sms_countries(request: Request) -> list[dict]:
        return await request.app.state.account_pipeline.hero_sms_countries()

    @app.post("/api/herosms/test")
    async def test_hero_sms(request: Request) -> dict:
        return await request.app.state.account_pipeline.hero_sms_test()

    @app.get("/api/pipeline")
    async def list_pipeline_items(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, alias="pageSize", ge=10, le=100),
        stage: str = Query(default="", max_length=64),
        q: str = Query(default="", max_length=320),
        export_state: str = Query(default="all", alias="exportState", pattern="^(all|exported|unexported)$"),
        settlement_state: str = Query(
            default="all",
            alias="settlementState",
            pattern="^(all|waiting|confirmed|review|failed)$",
        ),
    ) -> dict:
        return await request.app.state.account_pipeline.list_items(
            page=page,
            page_size=page_size,
            stage=stage,
            query=q,
            export_state=export_state,
            settlement_state=settlement_state,
        )

    @app.get("/api/pipeline/paid/stats")
    async def pipeline_paid_stats(
        request: Request,
        days: int = Query(default=14, ge=7, le=31),
    ) -> dict:
        return await request.app.state.account_pipeline.paid_stats(days=days)

    @app.post("/api/pipeline/paid/export")
    async def export_paid_pipeline_accounts(
        payload: PipelinePaidExportInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.export_paid(payload)

    @app.post("/api/pipeline/paid/export-status")
    async def update_paid_pipeline_export_status(
        payload: PipelinePaidExportStatusInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.mark_paid_export_status(payload)

    @app.post("/api/pipeline/paid/mail-check")
    async def check_paid_pipeline_mail(
        payload: PipelinePaidMailCheckInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.check_paid_mail(payload)

    @app.get("/api/pipeline/sms-receiver/settings")
    async def sms_receiver_settings(request: Request) -> dict:
        return await request.app.state.account_pipeline.sms_receiver_settings()

    @app.put("/api/pipeline/sms-receiver/settings")
    async def update_sms_receiver_settings(
        payload: SmsReceiverSettingsUpdate,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.update_sms_receiver_settings(payload)

    @app.post("/api/pipeline/sms-receiver/test")
    async def test_sms_receiver(request: Request) -> dict:
        return await request.app.state.account_pipeline.test_sms_receiver()

    @app.post("/api/pipeline/paid/sms-receiver/submit")
    async def submit_paid_to_sms_receiver(
        payload: SmsReceiverBatchInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.submit_paid_to_sms_receiver(payload)

    @app.post("/api/pipeline/paid/sms-receiver/status")
    async def refresh_sms_receiver_status(
        payload: SmsReceiverBatchInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.refresh_sms_receiver_status(payload)

    @app.post("/api/pipeline/sync")
    async def sync_pipeline(request: Request) -> dict:
        result = await request.app.state.account_pipeline.sync_eligible()
        await request.app.state.account_pipeline.tick()
        return result

    @app.post("/api/pipeline/extract", status_code=202)
    async def start_pipeline_extractions(
        payload: PipelineBatchInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.start_extractions(payload.ids)

    @app.post("/api/pipeline/herosms/test")
    async def test_pipeline_hero_sms(request: Request) -> dict:
        return await request.app.state.account_pipeline.hero_sms_test()

    @app.post("/api/pipeline/{item_id}/payment", status_code=202)
    async def start_pipeline_payment(
        item_id: str,
        payload: PipelinePaymentInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.start_payment(item_id, payload)

    @app.post("/api/pipeline/{item_id}/otp")
    async def submit_pipeline_otp(
        item_id: str,
        payload: PipelineOtpInput,
        request: Request,
    ) -> dict:
        return await request.app.state.account_pipeline.submit_otp(item_id, payload)

    @app.post("/api/pipeline/{item_id}/retry-extraction")
    async def retry_pipeline_extraction(item_id: str, request: Request) -> dict:
        return await request.app.state.account_pipeline.reset_stage(item_id, "extraction")

    @app.post("/api/pipeline/{item_id}/retry-payment")
    async def retry_pipeline_payment(item_id: str, request: Request) -> dict:
        return await request.app.state.account_pipeline.reset_stage(item_id, "payment")

    @app.delete("/api/pipeline/{item_id}")
    async def delete_pipeline_item(item_id: str, request: Request) -> dict:
        return {"deleted": await request.app.state.account_pipeline.delete(item_id)}

    @app.get("/api/paypal-agreement/source")
    async def paypal_agreement_source(request: Request) -> dict:
        service: PaypalAgreementService = request.app.state.paypal_agreement_service
        return {
            "service": "paypal-agreement-protocol",
            "sourceCommit": service.source_commit,
            "uiPath": service.ui_path,
            "isolated": True,
        }

    @app.get("/paypal-pay")
    async def paypal_agreement_root_redirect() -> RedirectResponse:
        return RedirectResponse("/paypal-pay/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.api_route(
        "/paypal-pay/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def paypal_agreement_proxy(path: str, request: Request) -> Response:
        service: PaypalAgreementService = request.app.state.paypal_agreement_service
        try:
            await asyncio.to_thread(service.start)
        except PaypalAgreementServiceError as exc:
            raise _paypal_agreement_error(exc) from exc

        upstream_url = f"{service.base_url}/{path}"
        if request.url.query:
            upstream_url = f"{upstream_url}?{request.url.query}"
        forward_headers: dict[str, str] = {}
        for header_name in ("accept", "accept-language", "cache-control", "content-type", "cookie", "user-agent"):
            value = request.headers.get(header_name)
            if value:
                forward_headers[header_name] = value
        # The sidecar validates same-origin POSTs against its own Host.  The
        # reverse proxy is the trusted local boundary, so never forward the
        # browser's Origin/Referer values from the outer app.
        forward_headers["host"] = f"{service.host}:{service.port}"
        body = await request.body()
        hero_context: dict | None = None
        if request.method == "POST" and path.rstrip("/") == "api/jobs":
            try:
                incoming_payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                incoming_payload = None
            if isinstance(incoming_payload, dict):
                incoming_payload, hero_context = await request.app.state.account_pipeline.prepare_agreement_hero_sms(
                    incoming_payload
                )
                body = json.dumps(incoming_payload, ensure_ascii=False).encode("utf-8")
                forward_headers["content-type"] = "application/json"
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False) as client:
                upstream = await client.request(
                    request.method,
                    upstream_url,
                    headers=forward_headers,
                    content=body,
                )
        except httpx.HTTPError as exc:
            await request.app.state.account_pipeline.cancel_prepared_agreement_hero_sms(
                hero_context
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "paypal_agreement_proxy_error", "message": str(exc)},
            ) from exc

        if hero_context is not None:
            if upstream.status_code >= 400:
                await request.app.state.account_pipeline.cancel_prepared_agreement_hero_sms(
                    hero_context
                )
            else:
                try:
                    upstream_payload = upstream.json()
                except ValueError:
                    upstream_payload = {}
                job = upstream_payload.get("job") if isinstance(upstream_payload, dict) else None
                job_id = str(job.get("id") or "") if isinstance(job, dict) else ""
                cookie = request.headers.get("cookie", "")
                if not cookie:
                    for value in upstream.headers.get_list("set-cookie"):
                        match = re.search(r"paypal_web_device_id=([^;]+)", value)
                        if match:
                            cookie = f"paypal_web_device_id={match.group(1)}"
                            break
                if job_id:
                    request.app.state.account_pipeline.track_agreement_hero_sms(
                        job_id, cookie, hero_context
                    )
                else:
                    await request.app.state.account_pipeline.cancel_prepared_agreement_hero_sms(
                        hero_context
                    )

        response_headers: dict[str, str] = {}
        for header_name in ("content-type", "cache-control", "content-disposition", "etag", "last-modified"):
            value = upstream.headers.get(header_name)
            if value:
                response_headers[header_name] = value
        if "content-security-policy" in upstream.headers:
            response_headers["content-security-policy"] = _rewrite_paypal_agreement_csp(
                upstream.headers["content-security-policy"]
            )
        location = upstream.headers.get("location")
        if location:
            response_headers["location"] = location.replace(service.base_url, "", 1) or "/paypal-pay/"
        result = Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )
        for cookie in upstream.headers.get_list("set-cookie"):
            # Keep the sidecar's device cookie scoped to its integration path.
            scoped_cookie = re.sub(r"(?i)path=/($|;)", r"Path=/paypal-pay/\1", cookie, count=1)
            result.headers.append("set-cookie", scoped_cookie)
        # The source's standalone X-Frame-Options header is deliberately not
        # copied; framing is allowed only through this same-origin local proxy.
        return result

    @app.post(
        "/api/tools/access-tokens/extract",
        response_model=AccessTokenExtractResult,
    )
    def extract_access_token_payload(
        payload: AccessTokenExtractInput,
    ) -> AccessTokenExtractResult:
        return extract_access_tokens(payload.rawText)

    def extractor_error(exc: PaymentExtractorServiceError) -> HTTPException:
        return HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        )

    @app.get("/api/payment-extractor/defaults")
    def payment_extractor_defaults(request: Request) -> dict:
        return request.app.state.payment_extractor_service.options()

    @app.put("/api/payment-extractor/concurrency")
    def payment_extractor_concurrency(
        payload: PaymentExtractorConcurrencyUpdate,
        request: Request,
    ) -> dict:
        try:
            return request.app.state.payment_extractor_service.set_concurrency(payload)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/proxy-test")
    async def payment_extractor_proxy_test(
        payload: PaymentExtractorProxyTest,
        request: Request,
    ) -> dict:
        try:
            return await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_test,
                payload,
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/proxy-source")
    async def payment_extractor_proxy_source(
        payload: PaymentExtractorProxySource,
        request: Request,
    ) -> dict:
        try:
            return await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_source,
                payload,
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks", status_code=202)
    def create_payment_extractor_task(
        payload: PaymentExtractorTaskCreate,
        request: Request,
    ) -> dict:
        try:
            return _task_response(
                request.app.state.payment_extractor_service.create(payload)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.get("/api/payment-extractor/tasks")
    def list_payment_extractor_tasks(request: Request) -> dict:
        return {
            "ok": True,
            "tasks": request.app.state.payment_extractor_service.list(),
        }

    @app.get("/api/payment-extractor/tasks/{task_id}")
    def get_payment_extractor_task(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.get(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/{task_id}/cancel")
    def cancel_payment_extractor_task(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.cancel(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/{task_id}/retry", status_code=202)
    def retry_payment_extractor_task(
        task_id: str,
        payload: PaymentExtractorTaskRetry,
        request: Request,
    ) -> dict:
        try:
            return _task_response(
                request.app.state.payment_extractor_service.retry(task_id, payload)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/{task_id}/resolve-paypal")
    def resolve_payment_extractor_paypal(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.resolve_paypal(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.delete("/api/payment-extractor/tasks/{task_id}")
    def delete_payment_extractor_task(task_id: str, request: Request) -> dict:
        try:
            return request.app.state.payment_extractor_service.delete(task_id)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/payment-extractor/tasks/bulk-delete")
    def bulk_delete_payment_extractor_tasks(
        payload: PaymentExtractorBulkDelete,
        request: Request,
    ) -> dict:
        return request.app.state.payment_extractor_service.bulk_delete(payload)

    # Compatibility routes matching the standalone extractor README.  The
    # existing /api/payment-extractor/* routes above remain unchanged for the
    # AutoRegister frontend.
    @app.get("/api/defaults")
    def extractor_defaults_compat(request: Request) -> dict:
        return _snakeize(request.app.state.payment_extractor_service.options())

    @app.get("/api/proxy/source")
    async def extractor_proxy_source_compat(
        request: Request, url: str = Query(default="", max_length=4096)
    ) -> dict:
        try:
            result = await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_source,
                PaymentExtractorProxySource(url=url),
            )
            return _snakeize(result)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/proxy/test")
    async def extractor_proxy_test_compat(
        payload: PaymentExtractorProxyTest, request: Request
    ) -> dict:
        try:
            result = await asyncio.to_thread(
                request.app.state.payment_extractor_service.proxy_test, payload
            )
            return _snakeize(result)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.get("/api/tasks")
    def extractor_tasks_compat(request: Request) -> dict:
        return {
            "ok": True,
            "tasks": _snakeize(request.app.state.payment_extractor_service.list()),
        }

    @app.post("/api/tasks", status_code=202)
    def extractor_create_task_compat(
        payload: PaymentExtractorTaskCreate, request: Request
    ) -> dict:
        try:
            result = request.app.state.payment_extractor_service.create(payload)
            return _task_response(result, snake=True)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.get("/api/tasks/{task_id}")
    def extractor_get_task_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(request.app.state.payment_extractor_service.get(task_id))
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/{task_id}/cancel")
    def extractor_cancel_task_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(request.app.state.payment_extractor_service.cancel(task_id))
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def extractor_retry_task_compat(
        task_id: str, payload: PaymentExtractorTaskRetry, request: Request
    ) -> dict:
        try:
            result = request.app.state.payment_extractor_service.retry(task_id, payload)
            return _task_response(result, snake=True)
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/{task_id}/resolve-paypal")
    def extractor_resolve_paypal_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(
                request.app.state.payment_extractor_service.resolve_paypal(task_id)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.delete("/api/tasks/{task_id}")
    def extractor_delete_task_compat(task_id: str, request: Request) -> dict:
        try:
            return _snakeize(request.app.state.payment_extractor_service.delete(task_id))
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.post("/api/tasks/bulk-delete")
    def extractor_bulk_delete_compat(
        payload: PaymentExtractorBulkDelete, request: Request
    ) -> dict:
        try:
            return _snakeize(
                request.app.state.payment_extractor_service.bulk_delete(payload)
            )
        except PaymentExtractorServiceError as exc:
            raise extractor_error(exc) from exc

    @app.websocket("/ws/tasks")
    async def extractor_task_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        password = _web_password()
        service = websocket.app.state.payment_extractor_service
        try:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            supplied = ""
            if isinstance(message, dict):
                supplied = str(message.get("password") or "")
            valid_auth = (
                isinstance(message, dict)
                and message.get("type") == "auth"
                and hmac.compare_digest(supplied, password)
            )
            if not valid_auth:
                await websocket.send_json(
                    {"type": "auth.failed", "data": {"error": "unauthorized"}}
                )
                await websocket.close(code=1008)
                return
            await websocket.send_json({"type": "auth.ok"})
        except Exception:
            try:
                await websocket.close(code=1008)
            except Exception:
                pass
            return

        subscriber = None
        try:
            history, subscriber = service.subscribe()
            for event in history:
                await websocket.send_json(_snakeize(service.public_event(event)))
            loop = asyncio.get_running_loop()
            last_ping = loop.time()
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, True, 1.0)
                except queue.Empty:
                    if loop.time() - last_ping >= 15:
                        await websocket.send_json(
                            {"type": "task.ping", "task_id": "", "timestamp": "", "data": {}}
                        )
                        last_ping = loop.time()
                    continue
                await websocket.send_json(_snakeize(service.public_event(event)))
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            if subscriber is not None:
                try:
                    service.unsubscribe(subscriber)
                except Exception:
                    pass

    @app.get("/api/settings/execution", response_model=ExecutionSettings)
    def get_execution_settings(request: Request) -> ExecutionSettings:
        try:
            return request.app.state.settings_store.load_public()
        except CorruptSettingsError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_corrupted", "message": str(exc)},
            ) from exc

    @app.put("/api/settings/execution", response_model=ExecutionSettings)
    def put_execution_settings(
        payload: ExecutionSettingsInput,
        request: Request,
    ) -> ExecutionSettings:
        try:
            stored = request.app.state.settings_store.save(payload)
            return ExecutionSettings.from_stored(stored)
        except CorruptSettingsError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_corrupted", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "settings_write_failed", "message": "无法原子保存配置文件"},
            ) from exc

    @app.get("/api/accounts", response_model=Page[AccountRecord])
    async def list_accounts(
        page: PageNumber = 1,
        page_size: PageSizeOption = Query(PageSizeOption.TEN, alias="pageSize"),
        q: SearchQuery = "",
        promotion: Annotated[str, Query(max_length=32)] = "",
    ) -> Page[AccountRecord]:
        mongo.require_online()
        return await resource_store.list_accounts(page, int(page_size), q, promotion)  # type: ignore[arg-type]

    @app.post("/api/accounts", response_model=AccountRecord, status_code=201)
    async def create_account(payload: AccountCreate) -> AccountRecord:
        mongo.require_online()
        try:
            return await resource_service.create_account(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/accounts/bulk-delete", response_model=DeleteResult)
    async def delete_accounts(payload: BulkIdsInput) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_accounts(payload.ids)

    @app.post(
        "/api/accounts/check-promotion",
        response_model=AccountPlanCheckResult,
    )
    async def check_account_promotions(
        request: Request,
        payload: AccountPlanCheckInput,
    ) -> AccountPlanCheckResult:
        mongo.require_online()
        return await request.app.state.plan_check_service.check_accounts(payload.ids)

    @app.post("/api/accounts/export", response_model=TextExport)
    async def export_accounts(payload: AccountExportInput) -> TextExport:
        mongo.require_online()
        if payload.scope != "all" and not payload.ids:
            raise HTTPException(status_code=422, detail="选中导出必须提供账号 ID")
        return await resource_service.export_accounts(payload)

    @app.get("/api/emails", response_model=Page[EmailRecord])
    async def list_emails(
        page: PageNumber = 1,
        page_size: PageSizeOption = Query(PageSizeOption.TEN, alias="pageSize"),
        q: SearchQuery = "",
        source: Annotated[str, Query(pattern="^(all|standard|mailcom_alias)$")] = "all",
    ) -> Page[EmailRecord]:
        mongo.require_online()
        return await resource_store.list_emails(page, int(page_size), q, source)  # type: ignore[arg-type]

    @app.post("/api/emails/import", response_model=ImportResult)
    async def import_emails(payload: RawImportInput) -> ImportResult:
        mongo.require_online()
        return await resource_service.import_emails(payload.rawText)

    @app.post("/api/emails/sync-mailcom-aliases", response_model=ImportResult)
    async def sync_mailcom_aliases() -> ImportResult:
        mongo.require_online()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                response = await client.get(
                    "http://127.0.0.1:3211/api/export/registration-items"
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "mailcom_hub_unavailable",
                    "message": "MailCom Hub 暂时不可用，请先启动本机邮箱管理器",
                },
            ) from exc
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "mailcom_hub_invalid_response",
                    "message": "MailCom Hub 返回格式无效",
                },
            )
        return await resource_service.sync_mailcom_aliases(items)

    @app.post("/api/emails/bulk-delete", response_model=DeleteResult)
    async def delete_emails(payload: BulkIdsInput) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_emails(payload.ids)

    @app.post("/api/emails/export", response_model=TextExport)
    async def export_emails(payload: EmailExportInput) -> TextExport:
        mongo.require_online()
        if payload.scope != "all" and not payload.ids:
            raise HTTPException(status_code=422, detail="选中导出必须提供邮箱 ID")
        return await resource_service.export_emails(payload)

    @app.get("/api/proxies", response_model=Page[ProxyRecord])
    async def list_proxies(
        page: PageNumber = 1,
        page_size: PageSizeOption = Query(PageSizeOption.TEN, alias="pageSize"),
        q: SearchQuery = "",
        country: Annotated[str, Query(max_length=2)] = "",
    ) -> Page[ProxyRecord]:
        mongo.require_online()
        return await resource_store.list_proxies(page, int(page_size), q, country)  # type: ignore[arg-type]

    @app.post("/api/proxies/import", response_model=ImportResult)
    async def import_proxies(payload: ProxyImportInput) -> ImportResult:
        mongo.require_online()
        return await resource_service.import_proxies(payload.rawText, payload.country, payload.group)

    @app.get("/api/proxies/countries", response_model=list[ProxyCountrySummary])
    async def proxy_countries() -> list[ProxyCountrySummary]:
        mongo.require_online()
        return await resource_store.proxy_country_summaries()

    @app.get("/api/proxies/groups", response_model=list[ProxyGroupSummary])
    async def proxy_groups() -> list[ProxyGroupSummary]:
        mongo.require_online()
        return await resource_store.proxy_group_summaries()

    @app.patch("/api/proxies/groups")
    async def update_proxy_group(payload: ProxyGroupUpdate) -> dict[str, int]:
        mongo.require_online()
        return await resource_store.update_proxy_group(
            payload.country,
            payload.group,
            new_country=payload.newCountry,
            new_group=payload.newGroup,
            enabled=payload.enabled,
        )

    @app.delete("/api/proxies/groups", response_model=DeleteResult)
    async def delete_proxy_group(
        country: Annotated[str, Query(min_length=2, max_length=2)],
        group: Annotated[str, Query(min_length=1, max_length=64)],
    ) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_proxy_group(country, group)

    @app.post("/api/proxies/bulk-delete", response_model=DeleteResult)
    async def delete_proxies(payload: BulkIdsInput) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_proxies(payload.ids)

    @app.delete("/api/proxies", response_model=DeleteResult)
    async def clear_proxies() -> DeleteResult:
        mongo.require_online()
        return await resource_store.clear_proxies()

    @app.patch("/api/proxies/{proxy_id}", response_model=ProxyRecord)
    async def update_proxy(proxy_id: str, payload: ProxyUpdate) -> ProxyRecord:
        mongo.require_online()
        return await resource_store.update_proxy(
            proxy_id,
            enabled=payload.enabled,
            country=payload.country,
            group=payload.group,
        )

    @app.delete("/api/proxies/{proxy_id}", response_model=DeleteResult)
    async def delete_proxy(proxy_id: str) -> DeleteResult:
        mongo.require_online()
        return await resource_store.delete_proxy(proxy_id)

    @app.get("/api/stats/overview", response_model=OverviewStats)
    async def overview_stats() -> OverviewStats:
        mongo.require_online()
        return await resource_store.overview_stats()

    @app.post("/api/runs/mock", response_model=RunState, status_code=202)
    async def start_mock_run(payload: MockRunCreate) -> RunState:
        try:
            return await run_manager.start(payload.count)
        except RunConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "run_conflict", "message": str(exc)},
            ) from exc
        except InsufficientEmailsError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "insufficient_emails", "message": str(exc)},
            ) from exc

    @app.post("/api/runs/browser-probe", response_model=RunState, status_code=202)
    async def start_browser_probe_run(payload: BrowserProbeRunCreate) -> RunState:
        try:
            return await run_manager.start_browser_probe(
                payload.count,
                payload.country,
                payload.group,
                payload.emailSource,
            )
        except RunConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "run_conflict", "message": str(exc)},
            ) from exc
        except InsufficientEmailsError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "insufficient_emails", "message": str(exc)},
            ) from exc
        except ProxyCountryUnavailableError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "proxy_country_unavailable",
                    "message": str(exc),
                    "country": exc.country,
                    "required": exc.required,
                    "available": exc.available,
                },
            ) from exc
        except RoxyWorkspaceMissingError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "roxy_workspace_missing",
                    "message": str(exc),
                    "available": 0,
                },
            ) from exc
        except RoxyApiError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": (
                        "roxy_auth_failed"
                        if exc.is_auth_failure
                        else "roxy_not_ready"
                    ),
                    "message": (
                        "Roxy API 凭据无效"
                        if exc.is_auth_failure
                        else "Roxy 尚未达到稳定就绪状态"
                    ),
                },
            ) from exc
    @app.get("/api/runs/active", response_model=RunState | None)
    async def active_mock_run() -> RunState | None:
        return await run_manager.active()

    @app.get("/api/runs/{run_id}", response_model=RunState)
    async def get_mock_run(run_id: str) -> RunState:
        try:
            return await run_manager.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": str(exc)},
            ) from exc

    @app.get("/api/runs/{run_id}/workers", response_model=list[WorkerSnapshot])
    async def get_run_workers(run_id: str) -> list[WorkerSnapshot]:
        try:
            return await run_manager.workers(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": str(exc)},
            ) from exc

    @app.post("/api/runs/{run_id}/cancel", response_model=RunState)
    async def cancel_mock_run(run_id: str) -> RunState:
        try:
            return await run_manager.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "message": str(exc)},
            ) from exc

    @app.get("/api/run-logs/runs", response_model=list[RunLogSummary])
    def list_run_logs(request: Request) -> list[RunLogSummary]:
        try:
            return request.app.state.run_log_store.list_runs()
        except CorruptRunLogError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_corrupted", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_read_failed", "message": "无法读取任务日志目录"},
            ) from exc

    @app.get("/api/run-logs/runs/{run_id}", response_model=RunLogFile)
    def get_run_log(run_id: UUID, request: Request) -> RunLogFile:
        try:
            return request.app.state.run_log_store.get_run(run_id)
        except RunLogNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_log_not_found", "message": str(exc)},
            ) from exc
        except CorruptRunLogError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_corrupted", "message": str(exc)},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "run_log_read_failed", "message": "无法读取任务日志文件"},
            ) from exc

    return app


app = create_app()
