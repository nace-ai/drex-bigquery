"""drex.decide and drex.decide_image against the mock Drex API and a fake Cloud Storage."""

import json

import pytest
from conftest import signed_url

QUESTIONS = {
    "refund": {"type": "noul", "instructions": "Is a refund needed?"},
    "team": {"type": "choice", "instructions": "Which team?", "criteria": ["billing", "tech"]},
    "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["low", "high"]},
}
COLOUR = {"which": {"type": "choice", "instructions": "Which colour is Image 1?", "criteria": ["red", "blue"]}}


def test_each_batch_logs_its_size_and_state_kinds_without_content(call, capsys):
    call(
        "decide", [[{"message": "charged twice"}, QUESTIONS, None], ["text", QUESTIONS, None], [None, QUESTIONS, None]]
    )
    (line,) = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "batch answered" in line]
    assert {k: line[k] for k in ("severity", "rows", "sent", "null_replies", "state_kinds")} == {
        "severity": "INFO",
        "rows": 3,
        "sent": 2,
        "null_replies": 1,
        "state_kinds": ["dict", "str"],
    }
    assert "charged" not in json.dumps(line)


def test_decide_returns_drex_s_whole_body(call, mock):
    body, status = call("decide", [[{"message": "charged twice"}, QUESTIONS, None], [None, QUESTIONS, None]])
    assert status == 200
    reply, null = body["replies"]
    assert null is None
    assert set(reply) >= {"model", "answers", "usage", "evaluation_time_ms", "request_id"}
    assert reply["answers"]["refund"] == {"type": "noul", "noul": 0.75, "confidence": 0.75}
    assert reply["answers"]["team"]["choice"] == "billing"
    (sent,) = mock.decisions()
    request = json.loads(sent["body"])
    assert request["state"] == {"message": "charged twice"}
    assert request["model"] == "drex-v2.0"
    # Sent in Drex's own form: a choice's labels as an object
    assert request["questions"]["team"]["criteria"] == {"billing": None, "tech": None}


def test_decide_takes_questions_written_as_a_json_string(call, mock):
    body, status = call("decide", [["text state", json.dumps(QUESTIONS), "drex-max"]])
    assert status == 200
    assert body["replies"][0]["answers"]["urgency"]["type"] == "score"
    assert mock.decisions()[0]["model"] == "drex-max"


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ({"__proto__": {"type": "noul", "instructions": "x"}}, "the question id '__proto__' is not allowed"),
        (
            {"a": {"type": "noul", "instructions": "x"}, "A": {"type": "noul", "instructions": "y"}},
            "question ids must be unique ignoring case; 'A' is a duplicate",
        ),
        ({" ": {"type": "noul", "instructions": "x"}}, "question ids must not be empty"),
        ({}, "questions takes 1 to 512 questions, got 0"),
        ({f"q{i}": {"type": "noul", "instructions": "x"} for i in range(513)}, "questions takes 1 to 512 questions"),
        ({"a": {"type": "multiple_choice", "criteria": ["x", "y"]}}, "questions.a: multiple_choice questions are not"),
        ({"a": {"type": "box"}}, "questions.a: box questions are not supported"),
        ({"a": {"type": "noul", "instructions": "x", "weight": 2}}, "questions.a has an unknown key 'weight'"),
        ({"a": {"type": "choice", "instructions": "x"}}, "questions.a needs criteria"),
        (["not", "an", "object"], "questions must be a JSON object of question id to question"),
        ('{"a": ', "questions is not valid JSON"),
    ],
)
def test_question_rules_fail_the_query(call, mock, questions, message):
    body, status = call("decide", [["fine", QUESTIONS, None], ["x", questions, None]])
    assert status == 400
    assert body["errorMessage"].startswith(f"drex.decide: {message}")
    assert mock.decisions() == []


def test_one_image_with_no_model_uses_drex_max(call, mock):
    body, status = call("decide_image", [["", signed_url("red.png"), COLOUR, None]])
    assert status == 200
    assert body["replies"][0]["answers"]["which"]["choice"] == "red"
    (sent,) = mock.decisions()
    assert (sent["model"], sent["image_count"], sent["image_colours"]) == ("drex-max", 1, ["red"])


@pytest.mark.parametrize(("images", "first"), [(["red.png", "blue.png"], "red"), (["blue.png", "red.png"], "blue")])
def test_two_images_keep_their_order(call, mock, images, first):
    body, status = call("decide_image", [["", [signed_url(i) for i in images], COLOUR, "drex-max"]])
    assert status == 200
    assert body["replies"][0]["answers"]["which"]["choice"] == first
    assert mock.decisions()[0]["image_colours"] == [i.removesuffix(".png") for i in images]


def test_images_on_a_text_model_fail_the_query(call, mock):
    body, status = call("decide_image", [["", signed_url("red.png"), COLOUR, "drex-v2.0"]])
    assert status == 400
    assert (
        body["errorMessage"] == "drex.decide_image: drex-v2.0 does not read images; use an image model such as drex-max"
    )
    assert mock.decisions() == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/red.png",
        "http://storage.googleapis.com/bucket/red.png",
        "https://storage.googleapis.com:8443/bucket/red.png",
        "https://storage.googleapis.com.evil.example/bucket/red.png",
        "https://user@storage.googleapis.com/bucket/red.png",
        "gs://bucket/red.png",
        # urlsplit takes these, httpx refuses them
        "https://storage.googleapis.com/bucket/red\x00.png",
        "https://storage.googleapis.com/" + "a" * 70_000,
        "https://storage.googleapis.com:port/bucket/red.png",
    ],
)
def test_only_signed_cloud_storage_urls_are_read(call, mock, storage, url):
    body, status = call("decide_image", [["", url, COLOUR, None]])
    assert status == 400
    assert body["errorMessage"].startswith(
        "drex.decide_image: images must be signed URLs on https://storage.googleapis.com/"
    )
    assert storage.requests == []
    assert mock.decisions() == []


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("notes.txt", "image 1 is not a PNG, JPEG or WebP image"),
        ("missing.png", "could not read image 1: HTTP 403"),
        ("huge.png", "image 1 is over 4194304 bytes"),
    ],
)
def test_an_unreadable_image_is_null_and_the_query_goes_on(call, mock, capsys, name, reason):
    body, status = call(
        "decide_image", [["", signed_url(name), COLOUR, None], ["", signed_url("blue.png"), COLOUR, None]]
    )
    assert status == 200
    assert body["replies"][0] is None
    assert body["replies"][1]["answers"]["which"]["choice"] == "blue"
    assert len(mock.decisions()) == 1
    out = capsys.readouterr().out
    assert reason in out
    # A signed URL is a credential
    assert "X-Goog-Signature" not in out


def test_a_failed_image_read_is_retried(call, storage):
    body, status = call("decide_image", [["", signed_url("flaky.png"), COLOUR, None]])
    assert status == 200
    assert body["replies"][0]["answers"]["which"]["choice"] == "blue"
    assert [r.url.path for r in storage.requests] == ["/bucket/flaky.png"] * 2


@pytest.mark.parametrize("images", [None, [], [signed_url("red.png"), None]])
def test_no_image_sends_nothing(call, mock, images):
    body, status = call("decide_image", [["", images, COLOUR, None]])
    assert (status, body) == (200, {"replies": [None]})
    assert mock.decisions() == []


def test_more_than_eight_images_fail_the_query(call, mock):
    body, status = call("decide_image", [["", [signed_url("red.png")] * 9, COLOUR, None]])
    assert status == 400
    assert body["errorMessage"] == "drex.decide_image: images takes 1 to 8 images, got 9"


def test_images_are_checked_when_the_questions_are_null(call, mock):
    body, status = call("decide_image", [["", "https://example.com/red.png", None, None]])
    assert status == 400
    assert body["errorMessage"].startswith("drex.decide_image: images must be signed URLs")
