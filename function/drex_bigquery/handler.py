"""BigQuery's remote-function protocol: one batch of calls in, one reply per call out.

BigQuery POSTs `{"requestId", "caller", "sessionUser", "userDefinedContext": {"fn"}, "calls": [[arg, ...], ...]}`
and takes `{"replies": [...]}` with 200, one reply per call in order, or `{"errorMessage"}` (under 1 KB) with any other
status, which fails the query. Every routine of the `drex` dataset calls this one function; `fn` names the routine.

BigQuery retries a whole batch on 408, 429, 500, 503 and 504, and every row Drex had already answered would be billed
again. So a failure is never answered with one of those: a Drex 429 or 5xx is retried here, inside the batch, and its
row is NULL once its retries run out; a fatal error, and any bug, answers 400.
"""

import asyncio
import json
import math
import os
import sys
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic_core import to_json

from drex_bigquery import CLIENT_NAME
from drex_bigquery.answers import answer_value
from drex_bigquery.drex import Drex, Lost, Settings, settings_from_env
from drex_bigquery.errors import FatalError
from drex_bigquery.images import MAX_IMAGES, download, image_urls
from drex_bigquery.questions import Question, json_argument, question, questions_from_json, to_wire

#: Requests in flight at once for one batch; the function's budget is max instances x container concurrency x this
DEFAULT_INFLIGHT = 8
#: A batch answers within this many seconds, with NULL for the rows still waiting, so that Cloud Run never times the
#: request out with a 504, which BigQuery would retry. Below the function's timeout (300 s by default)
DEFAULT_BATCH_DEADLINE_SECONDS = 240.0
#: BigQuery shows errorMessage only under 1 KB
MAX_ERROR_BYTES = 1000


@dataclass(frozen=True)
class Routine:
    arguments: int
    default_model: str
    #: The typed helpers' question type, and the name of their criteria argument
    question_type: str | None = None
    criteria_name: str = ""


ROUTINES = {
    "noul": Routine(4, "drex-v2.0", "noul", "criteria"),
    "choice": Routine(4, "drex-v2.0", "choice", "labels"),
    "score": Routine(4, "drex-v2.0", "score", "levels"),
    "decide": Routine(3, "drex-v2.0"),
    "decide_image": Routine(4, "drex-max"),
}


@dataclass
class Row:
    """One call that sends a request."""

    index: int
    model: str
    state: Any
    questions: dict[str, Any]
    #: The typed helpers' one question, whose answer is mapped to the helper's result
    question: Question | None = None
    images: list[str] | None = None


def log(severity: str, message: str, **fields: Any) -> None:
    """A structured log line, which Cloud Logging reads from stdout. Never a row's content."""
    print(json.dumps({"severity": severity, "message": message, **fields}), file=sys.stdout, flush=True)


def _env_number(environ: Mapping[str, str], name: str, default: float) -> float:
    text = environ.get(name, "").strip()
    try:
        value = float(text) if text else default
    except ValueError:
        raise FatalError(f"drex: {name} must be a number") from None
    if not math.isfinite(value) or value <= 0:
        raise FatalError(f"drex: {name} must be positive")
    return value


def _model(value: Any, routine: Routine, function: str) -> str:
    if value is None or value == "":
        return routine.default_model
    if not isinstance(value, str):
        raise FatalError(f"{function}: model must be a STRING")
    return value


def plan(function: str, routine: Routine, calls: list[Any]) -> tuple[list[Row | None], set[str]]:
    """The request each call sends, None for a call that sends nothing, and every model the calls name. Each distinct
    question or label set is checked once; one that fails a check fails the query, whichever row holds it."""
    rows: list[Row | None] = []
    models: set[str] = set()
    typed: dict[tuple[str, str], tuple[Question, dict[str, Any]]] = {}
    decided: dict[str, dict[str, Any]] = {}
    for index, call in enumerate(calls):
        if not isinstance(call, list) or len(call) != routine.arguments:
            raise FatalError(f"{function}: expected {routine.arguments} arguments per call")
        model = _model(call[-1], routine, function)
        models.add(model)
        if routine.question_type is not None:
            text, instructions, criteria = call[0], call[1], json_argument(function, routine.criteria_name, call[2])
            if instructions is not None and not isinstance(instructions, str):
                raise FatalError(f"{function}: instructions must be a STRING")
            key = (instructions or "", to_json(criteria).decode())
            if key not in typed:
                one = question(function, routine.question_type, instructions or "", criteria, routine.criteria_name)
                typed[key] = (one, to_wire([one]))
            one, wire = typed[key]
            if text is None:
                rows.append(None)
                continue
            if not isinstance(text, str):
                raise FatalError(f"{function}: input must be a STRING")
            rows.append(Row(index, model, text, wire, question=one))
            continue

        state, questions = call[0], json_argument(function, "questions", call[-2])
        images = None
        if function == "drex.decide_image":
            images = image_urls(function, json_argument(function, "images", call[1]))
            if images is not None and len(images) > MAX_IMAGES:
                raise FatalError(f"{function}: images takes 1 to {MAX_IMAGES} images, got {len(images)}")
        if questions is None:
            rows.append(None)
            continue
        key = to_json(questions).decode()
        if key not in decided:
            decided[key] = to_wire(questions_from_json(function, questions))
        if state is None or (function == "drex.decide_image" and images is None):
            rows.append(None)
            continue
        rows.append(Row(index, model, state, decided[key], images=images))
    return rows, models


async def run_batch(
    payload: Any,
    settings: Settings,
    inflight: int,
    deadline_seconds: float,
    http: httpx.AsyncClient,
    storage: httpx.AsyncClient,
) -> list[Any]:
    """The replies to one batch, in order."""
    context = payload.get("userDefinedContext") if isinstance(payload, dict) else None
    fn = context.get("fn") if isinstance(context, dict) else None
    if fn not in ROUTINES:
        raise FatalError(f"drex: userDefinedContext.fn must be one of {', '.join(ROUTINES)}")
    function, routine = f"drex.{fn}", ROUTINES[fn]
    calls = payload.get("calls")
    if not isinstance(calls, list):
        raise FatalError(f"{function}: the request has no calls")
    request_id = payload.get("requestId")

    # The deadline counts from here, the model checks included, so the batch answers before Cloud Run times out
    started = time.monotonic()
    rows, models = plan(function, routine, calls)
    slots = asyncio.Semaphore(inflight)
    drex = Drex(function, settings, http, slots)
    # Every model is checked before anything is sent, a NULL row's too
    try:
        async with asyncio.timeout(deadline_seconds):
            for model in sorted(models):
                listed = await drex.model(model)
                if fn == "decide_image" and not listed.reads_images:
                    raise FatalError(f"{function}: {model} does not read images; use an image model such as drex-max")
    except TimeoutError:
        raise FatalError(f"{function}: could not check the model in the batch's {deadline_seconds:g} s") from None

    def lost(row: Row, reason: str) -> None:
        log("WARNING", f"{function}: a row returned NULL: {reason}", bigquery_request_id=request_id, row=row.index)

    async def answer(row: Row) -> Any:
        """The row's reply, or None once its warning is logged."""
        images = []
        for number, url in enumerate(row.images or [], 1):
            image = await download(storage, url, number, slots)
            if isinstance(image, Lost):
                lost(row, image.reason)
                return None
            images.append(image)
        body = await drex.decide(row.model, row.state, row.questions, images)
        if isinstance(body, Lost):
            lost(row, body.reason)
            return None
        return body if row.question is None else answer_value(function, row.question, body)

    replies: list[Any] = [None] * len(calls)
    tasks = {asyncio.create_task(answer(row)): row for row in rows if row}
    if tasks:
        left = max(0.0, deadline_seconds - (time.monotonic() - started))
        done, pending = await asyncio.wait(tasks, timeout=left, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.exception() is not None:
                raise task.exception()
            replies[tasks[task].index] = task.result()
        for task in pending:
            lost(tasks[task], f"the batch ran out of time ({deadline_seconds:g} s); lower max_batching_rows")
    # One line per batch: its size and timing, and the JSON kind of each state as BigQuery encoded it. No content
    log(
        "INFO",
        f"{function}: batch answered",
        bigquery_request_id=request_id,
        rows=len(calls),
        sent=len(tasks),
        null_replies=sum(reply is None for reply in replies),
        seconds=round(time.monotonic() - started, 3),
        state_kinds=sorted({type(row.state).__name__ for row in rows if row}),
    )
    return replies


def _error_message(message: str) -> str:
    encoded = message.encode()
    if len(encoded) < MAX_ERROR_BYTES:
        return message
    return encoded[: MAX_ERROR_BYTES - 3].decode(errors="ignore") + "..."


def respond(
    payload: Any,
    environ: Mapping[str, str] = os.environ,
    transport: httpx.AsyncBaseTransport | None = None,
    storage_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], int]:
    """The response body and status for one BigQuery request. The transports are for tests."""
    try:
        settings = settings_from_env("drex", environ)
        inflight = _env_number(environ, "DREX_INFLIGHT_PER_REQUEST", DEFAULT_INFLIGHT)
        if inflight != int(inflight) or inflight < 1:
            raise FatalError("drex: DREX_INFLIGHT_PER_REQUEST (the concurrency) must be a whole number, at least 1")
        deadline = _env_number(environ, "DREX_BATCH_DEADLINE_SECONDS", DEFAULT_BATCH_DEADLINE_SECONDS)

        async def run() -> list[Any]:
            async with (
                httpx.AsyncClient(transport=transport) as http,
                httpx.AsyncClient(transport=storage_transport, headers={"User-Agent": CLIENT_NAME}) as storage,
            ):
                return await run_batch(payload, settings, int(inflight), deadline, http, storage)

        return {"replies": asyncio.run(run())}, 200
    except FatalError as error:
        log("ERROR", str(error), bigquery_request_id=_request_id(payload))
        return {"errorMessage": _error_message(str(error))}, 400
    except Exception:
        # A bug: answered 400, never 500, which BigQuery would retry and bill again
        log("ERROR", "drex: unexpected error", bigquery_request_id=_request_id(payload), trace=traceback.format_exc())
        return {"errorMessage": "drex: unexpected error in the Drex function; see its Cloud Run logs"}, 400


def _request_id(payload: Any) -> Any:
    return payload.get("requestId") if isinstance(payload, dict) else None
