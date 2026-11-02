"""The remote-function protocol, configuration, concurrency and the OpenRouter backend."""

import asyncio
import json
import ssl
import threading
import time
import urllib.request

import httpx
import pytest
from conftest import MOCK_KEY, Mock

from drex_bigquery import CLIENT_NAME, drex, handler


def test_an_unknown_routine_fails(call):
    body, status = call("translate", [["x"]])
    assert status == 400
    assert (
        body["errorMessage"] == "drex: userDefinedContext.fn must be one of noul, choice, score, decide, decide_image"
    )


def test_a_call_with_the_wrong_arguments_fails(call):
    body, status = call("noul", [["x", "Is it?", None]])
    assert (status, body) == (400, {"errorMessage": "drex.noul: expected 4 arguments per call"})


def test_an_empty_batch_sends_nothing(call, mock):
    assert call("noul", []) == ({"replies": []}, 200)
    assert mock.log() == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"NACE_API_KEY": ""}, "drex: NACE_API_KEY is not set on the Cloud Run function"),
        ({"NACE_BASE_URL": "http://console.nace.ai"}, "drex: NACE_BASE_URL must be an https:// URL"),
        ({"DREX_INFLIGHT_PER_REQUEST": "0"}, "drex: DREX_INFLIGHT_PER_REQUEST must be positive"),
        ({"DREX_INFLIGHT_PER_REQUEST": "2.5"}, "drex: DREX_INFLIGHT_PER_REQUEST (the concurrency) must be a whole"),
        ({"DREX_BATCH_DEADLINE_SECONDS": "soon"}, "drex: DREX_BATCH_DEADLINE_SECONDS must be a number"),
    ],
)
def test_configuration_errors_fail_the_query(call, env, message):
    body, status = call("noul", [["x", "Is it?", None, None]], env=env)
    assert status == 400
    assert body["errorMessage"].startswith(message)


def test_error_messages_stay_under_1_kb(call):
    body, status = call("choice", [["x", "Which?", ["é" * 2000, "é" * 2000], None]])
    assert status == 400
    assert body["errorMessage"].endswith("...")
    assert len(body["errorMessage"].encode()) < 1000


def test_a_bug_answers_400_never_500(call, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise KeyError("boom")

    monkeypatch.setattr(handler, "plan", broken)
    body, status = call("noul", [["x", "Is it?", None, None]])
    assert (status, body["errorMessage"]) == (
        400,
        "drex: unexpected error in the Drex function; see its Cloud Run logs",
    )
    assert "KeyError" in capsys.readouterr().out


def test_the_batch_deadline_answers_in_time_with_nulls(call, mock, capsys):
    rows = [[f"row {i}", "Is it?", None, None] for i in range(4)]
    body, status = call("noul", rows, scenario="slow-3000", env={"DREX_BATCH_DEADLINE_SECONDS": "1"})
    assert (status, body) == (200, {"replies": [None] * 4})
    assert "the batch ran out of time (1 s)" in capsys.readouterr().out


class SlowModels(httpx.AsyncBaseTransport):
    """Holds each GET /v1/models `seconds` before passing it on."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            await asyncio.sleep(self.seconds)
        return await self.inner.handle_async_request(request)


def test_the_deadline_counts_the_model_check(call):
    started = time.monotonic()
    body, status = call(
        "noul", [["x", "Is it?", None, None]], env={"DREX_BATCH_DEADLINE_SECONDS": "1"}, transport=SlowModels(3)
    )
    assert (status, body) == (400, {"errorMessage": "drex.noul: could not check the model in the batch's 1 s"})
    assert time.monotonic() - started < 2


def test_the_rows_get_what_the_model_check_left_of_the_deadline(call, capsys):
    started = time.monotonic()
    body, status = call(
        "noul",
        [["x", "Is it?", None, None]],
        scenario="slow-3000",
        env={"DREX_BATCH_DEADLINE_SECONDS": "1.5"},
        transport=SlowModels(1),
    )
    assert (status, body) == (200, {"replies": [None]})
    assert time.monotonic() - started < 2.2
    assert "the batch ran out of time" in capsys.readouterr().out


def test_the_retry_budget_starts_at_the_first_attempt_not_in_the_queue(monkeypatch):
    monkeypatch.setattr(drex, "REQUEST_BUDGET_SECONDS", 0.5)
    answers = iter([drex.Response(429, headers={"retry-after-ms": "10"}), drex.Response(200)])

    async def send() -> drex.Response:
        return next(answers)

    async def run() -> drex.Response | drex.Lost:
        slots = asyncio.Semaphore(1)
        async with slots:  # another row's request, longer than the whole budget
            queued = asyncio.create_task(drex.with_retries(send, slots, lambda _: None))
            await asyncio.sleep(1)
        return await queued

    assert asyncio.run(run()) == drex.Response(200)


def test_an_unknown_model_on_a_null_row_fails_the_query(call, mock):
    body, status = call("noul", [[None, "Is it?", None, "drex-nope"], ["x", "Is it?", None, None]])
    assert status == 400
    assert body["errorMessage"].startswith("drex.noul: unknown model 'drex-nope'")
    assert mock.decisions() == []


class Refusing(httpx.AsyncBaseTransport):
    """Answers POST /v1/systemone with a 422 about both the state and the questions; passes the rest on."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            issues = [
                {"path": "state.patient_ssn_123456789", "message": "Expected string"},
                {"path": "questions.decide", "message": "bad"},
            ]
            return httpx.Response(422, json={"error": {"message": "Invalid request", "issues": issues}})
        return await self.inner.handle_async_request(request)


def test_a_refusal_never_quotes_the_state_s_keys(call, capsys):
    body, status = call(
        "decide",
        [[{"patient_ssn_123456789": 1}, {"decide": {"type": "noul", "instructions": "x"}}, None]],
        transport=Refusing(),
    )
    assert status == 400
    assert body["errorMessage"].startswith("drex.decide: Drex refused the request: state: Expected string; ")
    assert "patient_ssn" not in json.dumps(body) + capsys.readouterr().out


def test_the_cap_holds_within_a_batch(call, mock):
    rows = [[f"row {i}", "Is it?", None, None] for i in range(24)]
    body, status = call("noul", rows, scenario="slow-200", env={"DREX_INFLIGHT_PER_REQUEST": "4"})
    assert status == 200 and body["replies"] == [0.75] * 24
    assert max(r["in_flight"] for r in mock.decisions()) == 4


def test_the_cap_holds_across_concurrent_batches(call, mock):
    """Cloud Run's container concurrency runs batches side by side: the budget is their number times the cap."""
    results = []

    def batch(n):
        rows = [[f"batch {n} row {i}", "Is it?", None, None] for i in range(10)]
        results.append(call("noul", rows, scenario="slow-200", env={"DREX_INFLIGHT_PER_REQUEST": "2"}))

    threads = [threading.Thread(target=batch, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [status for _, status in results] == [200] * 3
    assert max(r["in_flight"] for r in mock.decisions()) == 6


@pytest.fixture
def open_router(mock_url):
    """The mock as OpenRouter, reached through it as a proxy with its self-signed certificate."""
    with urllib.request.urlopen(f"{mock_url}/__fixtures/openrouter.pem") as response:  # noqa: S310 - the local mock
        context = ssl.create_default_context(cadata=response.read().decode())
    return httpx.AsyncHTTPTransport(proxy=mock_url, verify=context)


def test_open_router_takes_its_own_body_and_headers(call, mock, open_router):
    env = {"NACE_API_KEY": "sk-or-v1-mockKey", "NACE_BASE_URL": "https://openrouter.ai/api"}
    state = f"look {mock.ns}"
    colour = {"which": {"type": "choice", "instructions": "Which colour is Image 1?", "criteria": ["red", "blue"]}}
    noul = {"true": "Yes", "false": None}
    assert call("noul", [[state, None, noul, None]], env=env, transport=open_router) == ({"replies": [0.75]}, 200)
    image = "https://storage.googleapis.com/bucket/red.png"
    body, status = call("decide_image", [[state, image, colour, None]], env=env, transport=open_router)
    assert status == 200
    assert body["replies"][0]["answers"]["which"]["choice"] == "red"

    # OpenRouter's requests are in the mock's default namespace
    noul_request, image_request = [r for r in Mock(mock.url, "default").decisions() if mock.ns in r["body"]]
    for record in (noul_request, image_request):
        assert record["open_router"] is True
        assert (record["http_referer"], record["x_openrouter_title"]) == (
            "https://nace.ai",
            "Drex integration for BigQuery",
        )
    # A question without instructions is sent with "", which OpenRouter requires
    assert json.loads(noul_request["body"])["questions"]["noul"]["instructions"] == ""
    sent = json.loads(image_request["body"])
    assert sent["model"] == "nace-ai/drex-max"
    assert "images" not in sent
    assert sent["state"][0] == {"type": "text", "text": state}
    assert sent["state"][1]["image_url"]["url"].startswith("data:image/png;base64,")


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests to `inner` and keeps each one's headers as sent."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.headers: list[httpx.Headers] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        return await self.inner.handle_async_request(request)


OPEN_ROUTER_ENV = {"NACE_API_KEY": "sk-or-v1-mockKey", "NACE_BASE_URL": "https://openrouter.ai/api"}
NOUL_ROW = [["x", None, {"true": "Yes", "false": None}, None]]


def test_open_router_attribution_is_set_by_the_caller(call, open_router):
    env = {**OPEN_ROUTER_ENV, "NACE_OPENROUTER_REFERER": "https://app.example", "NACE_OPENROUTER_TITLE": "Example"}
    recording = RecordingTransport(open_router)
    assert call("noul", NOUL_ROW, env=env, transport=recording)[1] == 200
    assert recording.headers
    for headers in recording.headers:
        assert headers.get_list("http-referer") == ["https://app.example"]
        assert headers.get_list("x-openrouter-title") == ["Example"]


def test_a_nace_host_gets_no_attribution_headers(call, mock):
    env = {"NACE_OPENROUTER_REFERER": "https://app.example", "NACE_OPENROUTER_TITLE": "Example"}
    assert call("noul", NOUL_ROW, env=env)[1] == 200
    assert all(r["http_referer"] is None and r["x_openrouter_title"] is None for r in mock.log())


def test_a_nace_key_is_never_sent_to_open_router(call, mock, open_router):
    recording = RecordingTransport(open_router)
    env = {**OPEN_ROUTER_ENV, "NACE_API_KEY": MOCK_KEY}
    body, status = call("noul", NOUL_ROW, env=env, transport=recording)
    assert status == 400
    assert "OPENROUTER_API_KEY" in body["errorMessage"]
    assert recording.headers == []
    assert MOCK_KEY not in json.dumps(body)


def test_open_router_prefers_its_own_key(call, mock, open_router):
    env = {**OPEN_ROUTER_ENV, "NACE_API_KEY": MOCK_KEY, "OPENROUTER_API_KEY": "sk-or-v1-fromOpenRouterVar"}
    assert call("noul", NOUL_ROW, env=env, transport=open_router)[1] == 200
    sent = {r["authorization_header"] for r in Mock(mock.url, "default").log() if r["endpoint"] == "/v1/systemone"}
    assert "Bearer sk-or-v1-fromOpenRouterVar" in sent


def test_an_open_router_key_in_nace_api_key_still_works(call, open_router):
    assert call("noul", NOUL_ROW, env=OPEN_ROUTER_ENV, transport=open_router)[1] == 200


def test_unknown_model_names_resolve_under_nace_ai_on_open_router(call, open_router):
    env = {"NACE_API_KEY": "sk-or-v1-mockKey", "NACE_BASE_URL": "https://openrouter.ai/api"}
    body, status = call("noul", [["x", "Is it?", None, "drex-max-v1.1"]], env=env, transport=open_router)
    assert status == 400
    assert body["errorMessage"].startswith("drex.noul: unknown model 'drex-max-v1.1'; GET /v1/models lists nace-ai/")


def test_requests_name_this_integration(call, mock):
    call("noul", [["x", "Is it?", None, None]])
    models, decision = mock.log()
    assert models["endpoint"] == "/v1/models"
    assert models["x_nace_client"] == decision["x_nace_client"] == CLIENT_NAME
    assert decision["authorization_header"] == f"Bearer {MOCK_KEY}"
    assert json.loads(decision["body"])["model"] == "drex-v2.0"
