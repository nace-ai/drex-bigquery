"""drex.noul, drex.choice and drex.score against the mock Drex API."""

import json

import pytest

from drex_bigquery import CLIENT_NAME

LABELS = ["tech", "billing"]
LEVELS = ["low", "medium", "high"]


@pytest.mark.parametrize("model", ["drex-v2.0", "drex-max"])
def test_noul_choice_and_score_on_each_model(call, mock, model):
    body, status = call("noul", [["I was charged twice", "Is this a refund request?", None, model]])
    assert (status, body) == (200, {"replies": [0.75]})

    body, status = call("choice", [["I was charged twice", "What is this about?", LABELS, model]])
    assert status == 200
    # The mock picks the first label, with weights 2 and 1; the rows are sorted by label
    assert body["replies"] == [
        {
            "choice": "tech",
            "probabilities": [{"value": "billing", "probability": 0.3333}, {"value": "tech", "probability": 0.6667}],
            "confidence": 0.9,
        }
    ]

    body, status = call("score", [["I was charged twice", "How urgent is it?", LEVELS, model]])
    assert status == 200
    assert body["replies"] == [
        {
            "score": 1.3333,
            "probabilities": [
                {"index": 0, "value": "low", "probability": 0.1667},
                {"index": 1, "value": "medium", "probability": 0.3333},
                {"index": 2, "value": "high", "probability": 0.5},
            ],
            "confidence": 0.8,
        }
    ]

    sent = mock.decisions()
    assert [r["model"] for r in sent] == [model] * 3
    assert {r["status"] for r in sent} == {200}
    assert {r["x_nace_client"] for r in sent} == {CLIENT_NAME}
    assert [json.loads(r["body"])["state"] for r in sent] == ["I was charged twice"] * 3
    assert json.loads(sent[1]["body"])["questions"] == {
        "choice": {"type": "choice", "instructions": "What is this about?", "criteria": {"tech": None, "billing": None}}
    }
    assert json.loads(sent[2]["body"])["questions"] == {
        "score": {"type": "score", "instructions": "How urgent is it?", "criteria": LEVELS}
    }


def test_mixed_batch_in_one_request(call, mock):
    body, status = call(
        "choice",
        [
            ["a", "Which team?", LABELS, None],
            [None, "Which team?", LABELS, None],
            ["b", "Which team?", ["x", "y", "z"], "drex-max"],
            ["c", None, LABELS, ""],
            ["d", "Which team?", '["tech", "billing"]', "drex-v2.0"],
        ],
    )
    assert status == 200
    replies = body["replies"]
    assert replies[1] is None
    assert [replies[i]["choice"] for i in (0, 2, 3, 4)] == ["tech", "x", "tech", "tech"]
    sent = mock.decisions()
    # A NULL input sends nothing; NULL and '' models are the default
    assert sorted(r["state"] for r in sent) == ['"a"', '"b"', '"c"', '"d"']
    assert sorted(r["model"] for r in sent) == ["drex-max", "drex-v2.0", "drex-v2.0", "drex-v2.0"]


def test_noul_with_criteria_and_no_instructions(call, mock):
    criteria = {"true": "The customer asks for money back", "false": None}
    body, status = call("noul", [["refund please", None, criteria, None]])
    assert (status, body) == (200, {"replies": [0.75]})
    (sent,) = mock.decisions()
    assert json.loads(sent["body"])["questions"] == {"noul": {"type": "noul", "instructions": "", "criteria": criteria}}


def test_choice_option_with_a_null_description(call, mock):
    labels = [{"label": "billing", "description": "Payments and invoices"}, {"label": "other", "description": None}]
    body, status = call("choice", [["x", "Which team?", labels, None]])
    assert status == 200
    assert body["replies"][0]["choice"] == "billing"
    (sent,) = mock.decisions()
    assert json.loads(sent["body"])["questions"]["choice"]["criteria"] == {
        "billing": "Payments and invoices",
        "other": None,
    }


def test_noul_labels_alone_are_no_criteria(call, mock):
    _body, status = call("noul", [["x", "Is it?", ["true", "false"], None]])
    assert status == 200
    (sent,) = mock.decisions()
    assert "criteria" not in json.loads(sent["body"])["questions"]["noul"]


@pytest.mark.parametrize(
    ("fn", "instructions", "criteria", "message"),
    [
        ("choice", "Which?", ["only"], "drex.choice: choice takes 2 to 255 labels, got 1"),
        ("choice", "Which?", ["a", "a"], "drex.choice: choice labels must be unique; 'a' is a duplicate"),
        ("choice", "Which?", ["a", ""], "drex.choice: choice labels must not be empty"),
        ("choice", "Which?", None, "drex.choice: labels must not be NULL"),
        ("choice", "Which?", [1, 2], "drex.choice: labels must be a JSON array of labels"),
        ("score", "How?", {"low": None, "high": None}, "drex.score: levels must be a JSON array of labels"),
        (
            "score",
            "How?",
            [{"label": "low", "description": "x"}, {"label": "high"}],
            "drex.score: score levels take no description",
        ),
        ("noul", "Is it?", ["yes", "no"], "drex.noul: noul labels must be exactly true and false, got [yes, no]"),
        ("noul", None, None, "drex.noul: noul needs instructions, or a description of true or false"),
        ("noul", "", ["true", "false"], "drex.noul: noul needs instructions"),
    ],
)
def test_label_rules_fail_the_query_before_any_request(call, mock, fn, instructions, criteria, message):
    body, status = call(
        fn,
        [
            ["fine", "Is it?", ["true", "false"] if fn == "noul" else ["a", "b"], None],
            ["x", instructions, criteria, None],
        ],
    )
    assert status == 400
    assert body["errorMessage"].startswith(message)
    assert mock.decisions() == []


def test_a_bad_label_set_fails_the_query_even_on_a_null_row(call, mock):
    _body, status = call("choice", [["x", "Which?", LABELS, None], [None, "Which?", ["only"], None]])
    assert status == 400
    assert mock.decisions() == []


def test_401_fails_the_query(call):
    body, status = call("noul", [["x", "Is it?", None, None]], scenario="401")
    assert status == 400
    assert body["errorMessage"].startswith("drex.noul: Invalid API key.")


def test_402_fails_the_query_with_the_billing_page(call, mock):
    body, status = call("noul", [["x", "Is it?", None, None]], scenario="402")
    assert status == 400
    message = body["errorMessage"]
    assert message.startswith("drex.noul: This account has insufficient available funds.")
    assert f"Billing: {mock.base_url('402')}/dashboard/billing" in message


def test_422_surfaces_each_issue(call):
    body, status = call("noul", [["x", "Is it?", None, None]], scenario="422")
    assert status == 400
    assert body["errorMessage"].startswith(
        "drex.noul: Drex refused the request: questions.a.instructions: must be a string; "
        "questions.a.instructions: noul needs instructions or criteria (request "
    )


def test_unknown_model_fails_the_query(call, mock):
    body, status = call("noul", [["x", "Is it?", None, "drex-nope"]])
    assert status == 400
    assert body["errorMessage"].startswith("drex.noul: unknown model 'drex-nope'; GET /v1/models lists ")
    assert "drex-max" in body["errorMessage"]
    assert mock.decisions() == []


@pytest.mark.parametrize(("scenario", "wait_ms"), [("429-once", 1000), ("529-once", 2000)])
def test_retried_after_retry_after_ms(call, mock, scenario, wait_ms):
    body, status = call("noul", [["x", "Is it?", None, None]], scenario=scenario)
    assert (status, body) == (200, {"replies": [0.75]})
    first, second = mock.decisions()
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert first["body"] == second["body"]
    assert wait_ms <= second["waited_ms"] < wait_ms + 1000


def test_exhausted_retries_are_null_never_a_5xx(call, mock, capsys):
    body, status = call("noul", [["x", "Is it?", None, None]], scenario="529-always")
    # BigQuery would retry a 5xx batch, and bill every answered row again
    assert (status, body) == (200, {"replies": [None]})
    assert [r["attempt"] for r in mock.decisions()] == [1, 2, 3, 4, 5]
    assert "no answer after 5 attempts, the last HTTP 529: Drex is at capacity." in capsys.readouterr().out


def test_an_oversized_row_is_null_and_the_query_goes_on(call, mock, capsys):
    secret = "confidential " * 400_000
    body, status = call("noul", [[secret, "Is it?", None, None], ["small", "Is it?", None, None]])
    assert (status, body) == (200, {"replies": [None, 0.75]})
    assert len(mock.decisions()) == 1
    out = capsys.readouterr().out
    assert "over Drex's limit of 4194304" in out
    assert "confidential" not in out


def test_a_row_over_the_token_limit_is_null(call, mock, capsys):
    body, status = call(
        "noul", [["long " * 2000, "Is it?", None, None], ["short", "Is it?", None, None]], scenario="too-long"
    )
    assert (status, body) == (200, {"replies": [None, 0.75]})
    out = capsys.readouterr().out
    assert "a row returned NULL: HTTP 422: state: exceeds the 1,024-token state limit" in out
    assert "long long" not in out
