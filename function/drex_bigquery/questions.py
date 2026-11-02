"""Questions: reading them from the routines' arguments, checking them as drex-duckdb does, and Drex's wire form.

Ported from drex-duckdb's `src/drex_functions.cpp` (`LabelsFromJSON`, `CheckLabels`, `CheckNoulHasText`,
`QuestionsFromJSON`, `QuestionsToJSON`), so every warehouse refuses the same questions with the same words.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

from drex_bigquery.errors import FatalError

QuestionType = Literal["noul", "choice", "score"]

#: Drex's limits: questions per request, and labels per choice or levels per score
MAX_QUESTIONS = 512
MIN_LABELS = 2
MAX_LABELS = 255


@dataclass(frozen=True)
class Label:
    """One label of a noul, choice or score: a plain label, or a label with a description."""

    label: str
    description: str | None = None


@dataclass(frozen=True)
class Question:
    id: str
    type: QuestionType
    instructions: str = ""
    labels: tuple[Label, ...] = ()
    #: Whether the labels were written as Drex's object of label to description, where a noul may describe only one
    labels_from_object: bool = False


def _shape_error(function: str, name: str, allow_object: bool) -> FatalError:
    shapes = "a JSON array of labels or of {label, description} objects"
    if allow_object:
        shapes += ", or an object of label to description"
    return FatalError(f"{function}: {name} must be {shapes}")


def json_argument(function: str, name: str, value: Any) -> Any:
    """A JSON argument as Python values.

    BigQuery encodes a JSON argument as the JSON value itself, as `TO_JSON_STRING` does. A string that holds a JSON
    array or object is read as that JSON too, so a caller who passes `'["a","b"]'` through `PARSE_JSON` or not gets
    the same answer.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                return json.loads(text)
            except ValueError:
                raise FatalError(f"{function}: {name} is not valid JSON") from None
    return value


def _text_or_null(shape_error: FatalError, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise shape_error
    return value


def _label(function: str, name: str, label: str | None, description: str | None) -> Label:
    if label is None:
        raise FatalError(f"{function}: {name} labels must not be NULL")
    return Label(label, description)


def labels_from_json(function: str, name: str, value: Any, allow_object: bool) -> tuple[list[Label], bool]:
    """The labels of criteria written in JSON, and whether they were an object of label to description.

    An array of labels, an array of `{label, description}` objects, or, for a noul or a choice, Drex's own object of
    label to description.
    """
    shape = _shape_error(function, name, allow_object)
    labels = []
    if isinstance(value, list):
        for element in value:
            if isinstance(element, dict):
                if not set(element) <= {"label", "description"}:
                    raise shape
                labels.append(
                    _label(
                        function,
                        name,
                        _text_or_null(shape, element.get("label")),
                        _text_or_null(shape, element.get("description")),
                    )
                )
            else:
                labels.append(_label(function, name, _text_or_null(shape, element), None))
        return labels, False
    if isinstance(value, dict) and allow_object:
        for key, description in value.items():
            labels.append(_label(function, name, key, _text_or_null(shape, description)))
        return labels, True
    raise shape


def check_labels(function: str, name: str, question: Question) -> None:
    """Checks a question's labels against Drex's and prompt_jev's rules."""
    labels = question.labels
    if question.type == "noul":
        # No labels is a noul without criteria
        names = [label.label for label in labels]
        valid = all(n in ("true", "false") for n in names) and len(set(names)) == len(names)
        complete = not labels or question.labels_from_object or {"true", "false"} <= set(names)
        if not (valid and complete):
            raise FatalError(f"{function}: {name} labels must be exactly true and false, got [{', '.join(names)}]")
        return
    kind = "labels" if question.type == "choice" else "levels"
    if not MIN_LABELS <= len(labels) <= MAX_LABELS:
        raise FatalError(f"{function}: {name} takes {MIN_LABELS} to {MAX_LABELS} {kind}, got {len(labels)}")
    seen = set()
    for label in labels:
        if not label.label:
            raise FatalError(f"{function}: {name} {kind} must not be empty")
        if label.label in seen:
            raise FatalError(f"{function}: {name} {kind} must be unique; '{label.label}' is a duplicate")
        seen.add(label.label)
        if question.type == "score" and label.description is not None:
            raise FatalError(f"{function}: score levels take no description; describe them in instructions")


def check_noul_has_text(function: str, name: str, question: Question) -> None:
    """Checks what Drex would refuse in a noul: one with neither instructions nor a description of true or false."""
    if question.type != "noul" or question.instructions:
        return
    if any(label.description for label in question.labels):
        return
    raise FatalError(f"{function}: {name} needs instructions, or a description of true or false")


def parse_question_type(function: str, name: str, value: Any) -> QuestionType:
    if value in ("noul", "choice", "score"):
        return value
    if value in ("multiple_choice", "box"):
        raise FatalError(f"{function}: {name}: {value} questions are not supported, since Drex has no {value} question")
    raise FatalError(
        f"{function}: {name}.type must be noul, choice or score"
        + (f", not '{value}'" if isinstance(value, str) else "")
    )


def question(function: str, question_type: QuestionType, instructions: str, criteria: Any, name: str) -> Question:
    """The one question of a typed helper (`noul`, `choice`, `score`), its criteria named `name` in errors."""
    labels: list[Label] = []
    from_object = False
    if criteria is not None:
        labels, from_object = labels_from_json(function, name, criteria, question_type != "score")
    elif question_type != "noul":
        raise FatalError(f"{function}: {name} must not be NULL")
    result = Question(question_type, question_type, instructions, tuple(labels), from_object)
    _check(function, [result], named=False)
    return result


def questions_from_json(function: str, value: Any) -> list[Question]:
    """The questions of `drex.decide`: a JSON object of question id to `{type, instructions, criteria}`."""
    if not isinstance(value, dict):
        raise FatalError(f"{function}: questions must be a JSON object of question id to question")
    questions = []
    for question_id, entry in value.items():
        name = f"questions.{question_id}"
        if not isinstance(entry, dict):
            raise FatalError(f"{function}: {name} must be an object with type, instructions and criteria")
        for key in entry:
            if key not in ("type", "instructions", "criteria"):
                raise FatalError(f"{function}: {name} has an unknown key '{key}'; use type, instructions and criteria")
        question_type = parse_question_type(function, name, entry.get("type"))
        instructions = entry.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise FatalError(f"{function}: {name}.instructions must be a string")
        criteria = entry.get("criteria")
        labels: list[Label] = []
        from_object = False
        if criteria is not None:
            labels, from_object = labels_from_json(function, f"{name}.criteria", criteria, question_type != "score")
        elif question_type != "noul":
            raise FatalError(f"{function}: {name} needs criteria")
        questions.append(Question(question_id, question_type, instructions or "", tuple(labels), from_object))
    _check(function, questions, named=True)
    return questions


def _check(function: str, questions: list[Question], named: bool) -> None:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise FatalError(f"{function}: questions takes 1 to {MAX_QUESTIONS} questions, got {len(questions)}")
    ids = set()
    for q in questions:
        name = f"questions.{q.id}" if named else q.id
        if not q.id.strip():
            raise FatalError(f"{function}: question ids must not be empty")
        # Drex collects answers in a plain object, whose __proto__ is not a key, so it would bill the request and drop
        # the answer
        if q.id == "__proto__":
            raise FatalError(f"{function}: the question id '__proto__' is not allowed: Drex can't answer it")
        if q.id.lower() in ids:
            raise FatalError(f"{function}: question ids must be unique ignoring case; '{q.id}' is a duplicate")
        ids.add(q.id.lower())
        check_labels(function, name, q)
        check_noul_has_text(function, name, q)


def to_wire(questions: list[Question]) -> dict[str, dict[str, Any]]:
    """The `questions` object Drex takes, with `instructions` always a string (OpenRouter requires one)."""
    wire: dict[str, dict[str, Any]] = {}
    for q in questions:
        entry: dict[str, Any] = {"type": q.type, "instructions": q.instructions}
        if q.type == "score":
            entry["criteria"] = [label.label for label in q.labels]
        elif q.type == "choice":
            entry["criteria"] = {label.label: label.description for label in q.labels}
        elif any(label.description is not None for label in q.labels):
            # A noul's labels alone are the same as no criteria
            entry["criteria"] = {label.label: label.description for label in q.labels}
        wire[q.id] = entry
    return wire
