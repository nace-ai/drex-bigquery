"""The mock Drex API (tests/mock/drex_mock.py, from drex-duckdb) in a thread, and helpers that call the function as
BigQuery does."""

import json
import threading
import urllib.request
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pngs
import pytest
from drex_mock import make_server

from drex_bigquery.handler import respond

#: A well-formed test key, never a real one
MOCK_KEY = "nace_sk_mockKeyForDrexContractTests_222222222222222"

RED = pngs.solid_png(pngs.COLOURS["red"])
BLUE = pngs.solid_png(pngs.COLOURS["blue"])

#: The objects the fake Cloud Storage serves, by path
OBJECTS = {
    "/bucket/red.png": RED,
    "/bucket/blue.png": BLUE,
    "/bucket/notes.txt": b"not an image",
    "/bucket/huge.png": RED + b"\0" * (4 * 1024 * 1024),
}


def signed_url(name: str) -> str:
    return f"https://storage.googleapis.com/bucket/{name}?X-Goog-Signature=abc"


class FakeStorage(httpx.AsyncBaseTransport):
    """Cloud Storage's signed URLs: the objects above, 403 for any other, and 503 on the first read of `flaky.png`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/bucket/flaky.png":
            first = sum(r.url.path == path for r in self.requests) == 1
            return httpx.Response(503, headers={"retry-after": "0"}) if first else httpx.Response(200, content=BLUE)
        if path in OBJECTS:
            return httpx.Response(200, content=OBJECTS[path])
        return httpx.Response(403, content=b"<Error>SignatureDoesNotMatch</Error>")


class Mock:
    """The running mock, and one test's namespace in it."""

    def __init__(self, url: str, ns: str | None = None) -> None:
        self.url = url
        self.ns = ns or uuid.uuid4().hex[:12]

    def base_url(self, scenario: str = "ok") -> str:
        return f"{self.url}/ns/{self.ns}/{scenario}"

    def log(self) -> list[dict[str, Any]]:
        """This namespace's API requests, in order."""
        with urllib.request.urlopen(f"{self.url}/__log/{self.ns}") as response:  # noqa: S310 - the local mock
            records = [json.loads(line) for line in response.read().decode().splitlines() if line]
        return [r for r in records if r["endpoint"] != "(log)"]

    def decisions(self) -> list[dict[str, Any]]:
        return [r for r in self.log() if r["endpoint"] == "/v1/systemone"]


@pytest.fixture(scope="session")
def mock_url() -> str:
    server = make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def mock(mock_url: str) -> Mock:
    return Mock(mock_url)


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


Call = Callable[..., tuple[dict[str, Any], int]]


@pytest.fixture
def call(mock: Mock, storage: FakeStorage) -> Call:
    """Calls the function with one batch, as BigQuery does: `call("choice", [[input, instructions, labels, model]])`."""

    def run(
        fn: str,
        calls: list[list[Any]],
        scenario: str = "ok",
        env: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> tuple[dict[str, Any], int]:
        environ = {"NACE_API_KEY": MOCK_KEY, "NACE_BASE_URL": mock.base_url(scenario), **(env or {})}
        payload = {
            "requestId": uuid.uuid4().hex,
            "caller": "//bigquery.googleapis.com/projects/p/jobs/j",
            "sessionUser": "test@example.com",
            "userDefinedContext": {"fn": fn},
            "calls": calls,
        }
        return respond(payload, environ, transport=transport, storage_transport=storage)

    return run
