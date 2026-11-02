"""Drex's answer to a typed helper's question, as the shared SQL surface returns it.

Ported from drex-duckdb's `AnswerValue`: a noul is its bare probability, a choice's probabilities are rows sorted by
label, a score's are rows by index. Every value comes from the response; a missing one fails the query.
"""

import math
from typing import Any

from drex_bigquery.errors import FatalError
from drex_bigquery.questions import Question


def _number(function: str, question: Question, answer: dict[str, Any], key: str) -> float:
    value = answer.get(key) if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise FatalError(f"{function}: Drex's answer to question '{question.id}' has no number {key}")
    return value


def answer_value(function: str, question: Question, body: Any) -> float | dict[str, Any]:
    """The helper's result for Drex's response body `body`."""
    answers = body.get("answers") if isinstance(body, dict) else None
    answer = answers.get(question.id) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise FatalError(f"{function}: Drex's response has no answer to question '{question.id}'")
    if question.type == "noul":
        return _number(function, question, answer, "noul")
    probabilities = answer.get("probabilities")
    confidence = _number(function, question, answer, "confidence")
    if question.type == "choice":
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise FatalError(f"{function}: Drex's answer to question '{question.id}' has no choice")
        # Sorted by code point, which is UTF-8 byte order, as DuckDB sorts them
        rows = [
            {"value": label, "probability": _number(function, question, probabilities, label)}
            for label in sorted(label.label for label in question.labels)
        ]
        return {"choice": choice, "probabilities": rows, "confidence": confidence}
    rows = [
        {"index": index, "value": label.label, "probability": _number(function, question, probabilities, str(index))}
        for index, label in enumerate(question.labels)
    ]
    return {"score": _number(function, question, answer, "score"), "probabilities": rows, "confidence": confidence}
