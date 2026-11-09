<p align="center">
  <a href="https://nace.ai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/nace-logo-white.svg"><img src="docs/assets/nace-logo-black.svg" alt="Nace" height="40"></picture></a>
</p>

<h1 align="center">Drex integration for BigQuery</h1>

<p align="center"><b>Ask typed questions about your data through BigQuery remote functions.</b></p>

<p align="center">
  <a href="https://github.com/nace-ai/drex-bigquery/actions/workflows/CI.yml"><img src="https://img.shields.io/badge/tests-GitHub_Actions-2088FF" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

Ask typed questions about your data from BigQuery SQL. This repository deploys [Drex](https://console.nace.ai/docs)
as BigQuery remote functions, backed by a Cloud Run function, to classify text, score rows and read images, returning
probabilities as SQL values.

**Also from Drex:** [Drex Browser Use](https://github.com/nace-ai/drex-browser-harness-pub), an AI browser agent and MCP server. One sentence fills a DoorDash cart in 16 moves.

[Getting started](#getting-started) · [API reference](docs/API.md) · [Development](#development)

## What it does

| Function | Result | Use case |
| --- | --- | --- |
| `drex.noul` | `FLOAT64` probability of yes | Flag refund requests |
| `drex.choice` | `JSON`: label, probabilities, confidence | Route tickets to a team |
| `drex.score` | `JSON`: score, probabilities, confidence | Rank urgency on an ordered scale |
| `drex.decide` | `JSON` full response | Ask several questions per row |
| `drex.decide_image` | `JSON` response about Cloud Storage images | Classify photos or receipts |

## Getting started

You need a Google Cloud project with billing, Terraform 1.5 or later, the `gcloud` CLI, and a Nace API key from the
[Nace console](https://console.nace.ai).

### 1. Authenticate

```sh
gcloud auth login
gcloud auth application-default login
```

Use a project where you can enable APIs, deploy Cloud Run functions, create service accounts and grant IAM roles.
Review the [module](terraform/main.tf) for the resources and permissions it manages.

### 2. Store the key in Secret Manager

```sh
gcloud services enable secretmanager.googleapis.com --project=PROJECT_ID
gcloud secrets create nace-api-key --project=PROJECT_ID --replication-policy=automatic
# Paste the key, then Ctrl-D, so it stays out of your shell history
gcloud secrets versions add nace-api-key --project=PROJECT_ID --data-file=-
```

The module reads the secret but never creates it, so the key never enters Terraform state.

### 3. Deploy

In a new directory, save this as `main.tf`. Terraform downloads the module from this repository at a release tag:

```hcl
module "drex" {
  source = "git::https://github.com/nace-ai/drex-bigquery.git//terraform?ref=v0.1.0"

  project_id     = "PROJECT_ID"
  region         = "us-central1" # in the dataset's location
  location       = "US"          # where your tables are
  api_key_secret = "nace-api-key"
}
```

Replace `PROJECT_ID`, then run in that deployment directory:

```sh
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

`terraform apply` enables the APIs it uses, uploads and builds the Cloud Run function, and creates the BigQuery
connection, the dataset `drex` and its five routines. The connection's service account is the only caller allowed to
invoke the function. The first apply takes about ten minutes, including waits for new IAM grants to take effect. If
it still fails with "Access to bucket gcf-v2-sources-... denied", a grant took longer than usual: run `terraform apply`
again. For a few minutes after the first apply, queries can fail with a permission error until Cloud Run accepts the
connection's grant; run them again.

### 4. Query

Run in BigQuery with your deployment project selected and the query location matching the dataset:

```sql
SELECT
  id,
  drex.noul(body, 'Is the customer asking for a refund?', NULL, NULL) AS refund,
  drex.choice(body, 'Which team should handle this ticket?', JSON '["billing", "technical", "account"]', NULL) AS team
FROM UNNEST([
  STRUCT(1 AS id, 'I was charged twice. Please refund one.' AS body),
  STRUCT(2 AS id, 'How do I change my email address?' AS body)
]);
```

`refund` is a probability from 0 to 1; `team` is JSON with a choice, probabilities and confidence.
Model answers vary; NULL means no answer.

Users running queries need `roles/bigquery.connectionUser` on the connection, besides read access to the dataset. The
[API reference](docs/API.md) covers every function, images from object tables, errors, retries and the settings.

## Examples

### Score a row

```sql
SELECT drex.score('Our checkout is down.', 'How urgent is this ticket?',
                  JSON '["low", "medium", "high"]', NULL) AS urgency;
```

Read the numeric score with `LAX_FLOAT64(urgency.score)` after saving or selecting the result in a subquery.

### Ask several questions in one request

```sql
SELECT drex.decide(
  JSON '{"ticket": "I was charged twice. Please refund one."}',
  JSON '{"refund": {"type": "noul", "instructions": "Is a refund requested?"}, "team": {"type": "choice", "instructions": "Which team?", "criteria": {"billing": "Payments", "technical": "Bugs"}}}',
  NULL
) AS response;
```

The response includes `answers`, usage and a request id. For photos, use
[`decide_image` with an object table](docs/API.md#images); it requires Cloud Storage images and a read grant
for the connection's service account.

## Configuration and behavior

The model defaults to `drex-v2.0`, or `drex-max` for images. Every argument is required; use NULL for defaults.
The module defaults to one instance, one batch per instance and eight requests in flight per batch, with
50 text rows or one image row per batch. Raising all three concurrency settings multiplies requests in flight.

Invalid questions, authentication failures and unknown models fail the query. Row-specific failures return NULL
with a warning. Retryable failures get at most five attempts within 120 seconds. See
[errors](docs/API.md#errors) and [module configuration](docs/API.md#configuration), including OpenRouter settings.

## Requests, privacy and cost

The function sends each evaluated row, and the instructions and labels, to the configured API (`console.nace.ai` by
default). Only answered requests are billed, but BigQuery can send a batch again even after the function answered it,
and a query run again sends its rows again: both are billed again. Save answers with `CREATE TABLE ... AS SELECT`.

Google Cloud resources and queries can incur charges separately from Drex. Only send data you are authorized
to share with the configured API; a custom endpoint receives the key and row content.

Warnings in the function's logs name a row's position and the reason it is NULL, never its content. Image URLs, which
are credentials, are never logged.

## Development

```sh
git clone https://github.com/nace-ai/drex-bigquery.git
cd drex-bigquery
uv sync
uv run pytest                     # the function against the mock Drex API
uv run ruff check function tests
terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate
```

- `function/` is the Cloud Run function (`main.py`, the `drex_bigquery` package and `requirements.txt`), built on
  [`nace-sdk`](https://pypi.org/project/nace-sdk/).
- `terraform/` is the module.
- `tests/mock/` is drex-duckdb's mock of Drex's API (`drex_mock.py`, from `nace-ai/drex-duckdb` at `8b48eac`), which
  validates requests as Drex does and its answers against Drex's `openapi.json`.

## Support and contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and pull requests, and the
[API reference](docs/API.md) for the complete contract. Report bugs through
[GitHub Issues](https://github.com/nace-ai/drex-bigquery/issues), with your runtime version and a minimal example
using synthetic data. Remove keys, signed URLs and private data from reports.

## License and trademarks

[MIT](LICENSE). Nace artwork belongs to Nace AI. The software license does not grant trademark rights.

This integration is maintained by Nace AI. References to the platform describe compatibility,
not sponsorship or endorsement by its owner.

BigQuery and Google Cloud are trademarks of Google LLC.
