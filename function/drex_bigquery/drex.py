"""Sending decisions to Drex, with drex-duckdb's retry and error rules (`src/drex_client.cpp`).

Requests go through nace-sdk's `AsyncNaceClient.system_one` with the SDK's own retries off: retries are made here,
one attempt at a time, so that a request waiting to retry holds no in-flight slot, a server's `retry-after-ms` is
capped at 30 s, and a TLS failure is final at once. The rules:

- Fail the query: 401, 402, a 422 that isn't about the row alone, a TLS failure, an unknown model.
- NULL with a warning: a 422 about the row (`Request too long:`, or issues only under `state` or `images`), a body
  over 4 MiB, an image that isn't a PNG, JPEG or WebP, retries exhausted, and any other status.
- Retry: no response, 408, 429, 500, 502, 503, 504 and 529; at most 5 attempts within 120 s of the first.
"""

import asyncio
import base64
import os
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from nace_sdk import AsyncNaceClient, RetryPolicy
from nace_sdk._core.errors import NaceAPIConnectionError, NaceAPIError, NaceAPIResponseValidationError, NaceError
from pydantic import RootModel
from pydantic_core import to_json

from drex_bigquery import CLIENT_NAME
from drex_bigquery.errors import FatalError

DEFAULT_BASE_URL = "https://console.nace.ai"
REQUEST_TIMEOUT_SECONDS = 60.0
#: Attempts per request, all with the same body
MAX_ATTEMPTS = 5
MAX_RETRY_WAIT_SECONDS = 30.0
#: From a request's first attempt; a retry that would start after it is not made
REQUEST_BUDGET_SECONDS = 120.0
#: Drex refuses a larger body with a 413
MAX_BODY_BYTES = 4 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})

#: The versions that read images. Drex's GET /v1/models has no image field, so the list is kept here; a listed model
#: reads images when it is one of these or its alias_for names one. OpenRouter lists each model's modalities
IMAGE_MODELS = frozenset({"drex-max", "drex-max-v1.1"})

OPENROUTER_REFERER = "https://nace.ai"
OPENROUTER_TITLE = "Drex integration for BigQuery"
OPENROUTER_BILLING_URL = "https://openrouter.ai/settings/credits"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: The SDK's retries are off: `with_retries` makes them
_NO_RETRY = RetryPolicy(max_retries=0)


class _Body(RootModel[dict[str, Any]]):
    """Drex's response body as it came, so `drex.decide` returns it unchanged."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    #: `NACE_OPENROUTER_REFERER` and `NACE_OPENROUTER_TITLE`, "" for the defaults
    referer: str = ""
    title: str = ""

    @property
    def open_router(self) -> bool:
        return is_open_router_host(self.base_url)

    @property
    def open_router_headers(self) -> dict[str, str]:
        """The app identity OpenRouter attributes requests to: the caller's, else Nace's. Empty off OpenRouter."""
        if not self.open_router:
            return {}
        return {
            "HTTP-Referer": self.referer or OPENROUTER_REFERER,
            "X-OpenRouter-Title": self.title or OPENROUTER_TITLE,
        }


def is_open_router_host(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def settings_from_env(function: str, environ: Mapping[str, str] = os.environ) -> Settings:
    """`NACE_API_KEY` (from Secret Manager) and the optional `NACE_BASE_URL`, never a function argument. On an
    OpenRouter host the key is `OPENROUTER_API_KEY` when set, else `NACE_API_KEY`, and a `nace_` key is refused."""
    api_key = environ.get("NACE_API_KEY", "").strip()
    base_url = (environ.get("NACE_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")
    parts = urlsplit(base_url)
    # The key goes to this URL: only localhost may have it in the clear
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        raise FatalError(f"{function}: NACE_BASE_URL must be an https:// URL")
    if is_open_router_host(base_url):
        api_key = environ.get("OPENROUTER_API_KEY", "").strip() or api_key
        if api_key.startswith("nace_"):
            raise FatalError(
                f"{function}: a Nace API key (nace_...) is never sent to OpenRouter; set OPENROUTER_API_KEY to an "
                "OpenRouter key"
            )
    if not api_key:
        raise FatalError(f"{function}: NACE_API_KEY is not set on the Cloud Run function")
    return Settings(
        api_key,
        base_url,
        environ.get("NACE_OPENROUTER_REFERER", "").strip(),
        environ.get("NACE_OPENROUTER_TITLE", "").strip(),
    )


@dataclass
class Response:
    """What one attempt got."""

    #: 0 when no response arrived
    status: int
    body: Any = None
    headers: Mapping[str, str] | None = None
    #: Why no response arrived (a connection error or a timeout)
    transport_error: str = ""


@dataclass(frozen=True)
class Lost:
    """A request with no answer, and why: its row is NULL, with this as the warning."""

    reason: str


def _wait_seconds(text: str | None, scale: float) -> float | None:
    """A non-negative number of milliseconds (`scale` 0.001) or seconds (`scale` 1), capped, else None."""
    try:
        number = float((text or "").strip())
    except ValueError:
        return None
    return min(number * scale, MAX_RETRY_WAIT_SECONDS) if number >= 0 else None


def retry_wait(response: Response, retry: int) -> float:
    """The wait before retry number `retry` (1-based): what the response asked for, capped, else 1, 2, 4 and 8 s less
    up to 25 % jitter."""
    headers = response.headers or {}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        wait = _wait_seconds(headers.get(name), scale)
        if wait is not None:
            return wait
    backoff = float(1 << min(retry - 1, 4))
    return min(backoff - random.uniform(0, backoff / 4), MAX_RETRY_WAIT_SECONDS)


def _error(body: Any) -> tuple[str, list[tuple[str, str]], str]:
    """Drex's error envelope `{"error": {"message", "issues": [{"path", "message"}]}, "request_id"}`."""
    if not isinstance(body, dict):
        return "", [], ""
    request_id = body.get("request_id") if isinstance(body.get("request_id"), str) else ""
    error = body.get("error")
    if isinstance(error, str):
        return error, [], request_id
    if not isinstance(error, dict):
        return "", [], request_id
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    issues = [
        (str(issue.get("path") or ""), str(issue.get("message") or ""))
        for issue in error.get("issues") or []
        if isinstance(issue, dict)
    ]
    return message, issues, request_id


def _issue_text(path: str, message: str) -> str:
    return f"{path}: {message}" if path else message


def _is_under(path: str, field: str) -> bool:
    return path == field or path.startswith(field + ".")


def refuses_row(body: Any) -> bool:
    """Whether a 422 is about the row rather than the query's constants (questions, model): a request over the token
    limit (Drex's documented `Request too long:`), or issues only under `state` and `images`."""
    message, issues, _ = _error(body)
    if message.startswith("Request too long:"):
        return True
    return bool(issues) and all(_is_under(path, "state") or _is_under(path, "images") for path, _ in issues)


def describe_failure(response: Response) -> str:
    """The response's own message, else its status. A row refusal's paths under `state` would name the row's own JSON
    keys, so only `state` is kept."""
    if response.status == 0:
        return response.transport_error or "no response"
    message, issues, request_id = _error(response.body)
    if response.status == 422:
        message = "; ".join(_issue_text("state" if _is_under(p, "state") else p, m) for p, m in issues)
    description = f"HTTP {response.status}" + (f": {message}" if message else "")
    return description + (f" (request {request_id})" if request_id else "")


def _is_tls_failure(response: Response) -> bool:
    error = response.transport_error.lower()
    return response.status == 0 and ("ssl" in error or "certificate" in error)


def raise_if_fatal(function: str, settings: Settings, response: Response) -> None:
    """Fails the query for 401, 402, a 422 that is not about the row alone, and a TLS failure."""
    if _is_tls_failure(response):
        raise FatalError(f"{function}: could not reach Drex over TLS: {response.transport_error}")
    if response.status not in (401, 402, 422) or (response.status == 422 and refuses_row(response.body)):
        return
    message, issues, request_id = _error(response.body)
    request = f" (request {request_id})" if request_id else ""
    if response.status == 401:
        raise FatalError(f"{function}: {message or 'the API key was refused (HTTP 401)'}{request}")
    if response.status == 402:
        billing = OPENROUTER_BILLING_URL if settings.open_router else settings.base_url + "/dashboard/billing"
        raise FatalError(
            f"{function}: {message or 'the account has no funds left (HTTP 402).'} Billing: {billing}{request}"
        )
    # A path under state names the row's own keys, which are content
    texts = (
        "; ".join(_issue_text("state" if _is_under(p, "state") else p, m) for p, m in issues) or message or "HTTP 422"
    )
    raise FatalError(f"{function}: Drex refused the request: {texts}{request}")


async def with_retries(
    send: Callable[[], Awaitable[Response]],
    slots: asyncio.Semaphore,
    raise_if_fatal: Callable[[Response], None],
) -> Response | Lost:
    """Sends until an answer, a final failure or the end of the budget, which counts from the first attempt, not from
    the wait for a slot. Each attempt holds one of `slots`; a request waiting to retry holds none."""
    started: float | None = None
    attempt = 0
    while True:
        attempt += 1
        async with slots:
            started = started or time.monotonic()
            response = await send()
        if 200 <= response.status < 300:
            return response
        raise_if_fatal(response)
        retryable = response.status == 0 or response.status in RETRYABLE_STATUSES
        if retryable and attempt < MAX_ATTEMPTS:
            wait = retry_wait(response, attempt)
            if time.monotonic() + wait <= started + REQUEST_BUDGET_SECONDS:
                await asyncio.sleep(wait)
                continue
        if retryable:
            return Lost(f"no answer after {attempt} attempts, the last {describe_failure(response)}")
        return Lost(describe_failure(response))


def image_mime_type(image: bytes) -> str | None:
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(image) >= 12 and image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return None


@dataclass(frozen=True)
class ListedModel:
    #: The name a request sends
    name: str
    reads_images: bool


@dataclass(frozen=True)
class ModelList:
    models: tuple[ListedModel, ...]
    #: Whether the list is OpenRouter's, whose names are under nace-ai/
    open_router: bool

    def find(self, model: str) -> ListedModel | None:
        name = f"nace-ai/{model}" if self.open_router and "/" not in model else model
        return next((listed for listed in self.models if listed.name == name), None)


#: GET /v1/models per base URL, for the life of the instance; fetched again for a model it lacks, so a model released
#: since is found
_model_lists: dict[str, ModelList] = {}


def _parse_models(function: str, body: Any) -> ModelList:
    malformed = FatalError(f"{function}: GET /v1/models answered with an unexpected body")
    if isinstance(body, dict) and isinstance(body.get("models"), list):
        # Drex's: {"models": [{"name", "alias_for"}]}
        models = []
        for model in body["models"]:
            name = model.get("name") if isinstance(model, dict) else None
            if not isinstance(name, str):
                raise malformed
            alias_for = model.get("alias_for")
            served_by = alias_for if isinstance(alias_for, str) else name
            models.append(ListedModel(name, served_by in IMAGE_MODELS))
        return ModelList(tuple(models), open_router=False)
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        # OpenRouter's: {"data": [{"id", "architecture": {"input_modalities"}}]}, of which only Nace's models are Drex
        models = []
        for model in body["data"]:
            model_id = model.get("id") if isinstance(model, dict) else None
            if not isinstance(model_id, str):
                raise malformed
            if not model_id.startswith("nace-ai/"):
                continue
            architecture = model.get("architecture")
            modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
            models.append(ListedModel(model_id, isinstance(modalities, list) and "image" in modalities))
        return ModelList(tuple(models), open_router=True)
    raise malformed


class Drex:
    """One batch's view of Drex: the settings, an HTTP client, and the in-flight slots its requests share."""

    def __init__(self, function: str, settings: Settings, http: httpx.AsyncClient, slots: asyncio.Semaphore) -> None:
        self.function = function
        self.settings = settings
        self._http = http
        self._slots = slots
        self._sdk = AsyncNaceClient(
            api_key=settings.api_key,
            base_url=settings.base_url,
            http_client=http,
            retry=_NO_RETRY,
            timeout=REQUEST_TIMEOUT_SECONDS,
            client_info=CLIENT_NAME,
            headers=settings.open_router_headers,
        )

    def _raise_if_fatal(self, response: Response) -> None:
        raise_if_fatal(self.function, self.settings, response)

    async def model(self, model: str) -> ListedModel:
        """The listed model `model` is sent as. Drex answers any unregistered drex-* name with drex-v2.0, so only this
        check refuses one."""
        models = _model_lists.get(self.settings.base_url)
        if models is None or models.find(model) is None:
            models = await self._fetch_models()
            _model_lists[self.settings.base_url] = models
        listed = models.find(model)
        if listed is None:
            names = ", ".join(m.name for m in models.models)
            raise FatalError(f"{self.function}: unknown model '{model}'; GET /v1/models lists {names}")
        return listed

    async def _fetch_models(self) -> ModelList:
        url = self.settings.base_url + "/v1/models"
        params = {"output_modalities": "decisions"} if self.settings.open_router else None
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Accept": "application/json",
            "User-Agent": CLIENT_NAME,
            "X-Nace-Client": CLIENT_NAME,
            **self.settings.open_router_headers,
        }

        async def send() -> Response:
            try:
                response = await self._http.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            except httpx.HTTPError as error:
                return Response(0, transport_error=f"{type(error).__name__}: {error}")
            try:
                body = response.json()
            except ValueError:
                body = None
            return Response(response.status_code, body, response.headers)

        result = await with_retries(send, self._slots, self._raise_if_fatal)
        if isinstance(result, Lost):
            raise FatalError(f"{self.function}: could not list Drex's models: {result.reason}")
        return _parse_models(self.function, result.body)

    async def decide(
        self, model: str, state: Any, questions: dict[str, Any], images: list[bytes]
    ) -> dict[str, Any] | Lost:
        """Drex's response body for one request, or why there is none."""
        for index, image in enumerate(images, 1):
            if image_mime_type(image) is None:
                return Lost(f"image {index} is not a PNG, JPEG or WebP image")
        open_router = self.settings.open_router
        if open_router:
            # OpenRouter's body: the model under nace-ai/, and the images as data URLs in state content parts after
            # the state as text
            model = model if "/" in model else f"nace-ai/{model}"
            if images:
                text = state if isinstance(state, str) else to_json(state).decode()
                state = [{"type": "text", "text": text}] + [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image_mime_type(i)};base64,{base64.b64encode(i).decode()}"},
                    }
                    for i in images
                ]
            wire_images = None
        else:
            wire_images = [base64.b64encode(image).decode() for image in images] or None

        # The body as the SDK serializes it
        body = {"state": state, "model": model, "questions": questions}
        if wire_images is not None:
            body["images"] = wire_images
        size = len(to_json(body))
        if size > MAX_BODY_BYTES:
            return Lost(f"the request body is {size} bytes, over Drex's limit of {MAX_BODY_BYTES}")

        async def send() -> Response:
            try:
                body = await self._sdk.system_one(
                    state, questions, model=model, images=wire_images, response_model=_Body
                )
            except NaceAPIResponseValidationError:
                raise FatalError(f"{self.function}: Drex answered with a body that is not a JSON object") from None
            except NaceAPIError as error:
                return Response(error.status, error.body, error.headers)
            except NaceAPIConnectionError as error:
                return Response(0, transport_error=str(error))
            except NaceError as error:
                raise FatalError(f"{self.function}: {error}") from None
            return Response(200, body.root)

        result = await with_retries(send, self._slots, self._raise_if_fatal)
        return result if isinstance(result, Lost) else result.body
