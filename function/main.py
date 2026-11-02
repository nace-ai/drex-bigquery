"""The Cloud Run function's entry point, `drex`: BigQuery's remote functions in the `drex` dataset call it."""

import flask
import functions_framework

from drex_bigquery.handler import respond


@functions_framework.http
def drex(request: flask.Request) -> flask.Response:
    body, status = respond(request.get_json(silent=True))
    return flask.jsonify(body), status
