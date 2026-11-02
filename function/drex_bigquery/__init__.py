"""Drex integration for BigQuery: the Cloud Run function behind the `drex` dataset's remote functions."""

__version__ = "0.1.0"

#: Sent as `X-Nace-Client`, so Drex records which integration sent each request
CLIENT_NAME = f"drex-bigquery/{__version__}"
