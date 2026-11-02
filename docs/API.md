# SQL API

Deploy the routines with the [Terraform module](../README.md#getting-started). Each is a BigQuery remote function in
the module's dataset (`drex` by default), backed by one Cloud Run function.

BigQuery remote functions take no optional arguments and no `ARRAY` or `STRUCT` arguments, so every argument is
required, `model` is always last, and lists of labels and questions are `JSON`.

## Functions

```sql
drex.noul(input STRING, instructions STRING, criteria JSON, model STRING) -> FLOAT64
drex.choice(input STRING, instructions STRING, labels JSON, model STRING) -> JSON
drex.score(input STRING, instructions STRING, levels JSON, model STRING) -> JSON
drex.decide(state JSON, questions JSON, model STRING) -> JSON
drex.decide_image(state JSON, images JSON, questions JSON, model STRING) -> JSON
```

| Function | Result |
| --- | --- |
| `noul` | The probability, from 0 to 1, that the answer is yes |
| `choice` | `{"choice", "probabilities": [{"value", "probability"}], "confidence"}`, the probabilities sorted by label |
| `score` | `{"score", "probabilities": [{"index", "value", "probability"}], "confidence"}`, the probabilities by index |
| `decide`, `decide_image` | Drex's response body: `model`, `answers`, `usage`, `evaluation_time_ms` and `request_id` |

```sql
SELECT
  id,
  drex.noul(body, 'Is the customer asking for a refund?', NULL, NULL) AS refund,
  drex.choice(body, 'Which team should handle this ticket?', JSON '["billing", "technical", "account"]', NULL) AS team,
  drex.score(body, 'How urgent is this ticket?', JSON '["low", "medium", "high"]', 'drex-max') AS urgency
FROM support.tickets;
```

Read a field of a `JSON` result with `JSON_VALUE(team.choice)` or `LAX_FLOAT64(urgency.score)`.

### Arguments

- **`input`** is text, sent as a JSON string `state`. A NULL `input` returns NULL and sends nothing. For JSON state,
  use `drex.decide`.
- **`instructions`**: NULL or `''` means none. A `noul` needs instructions or a described `true` or `false`.
- **`criteria`, `labels`, `levels`** take a JSON array of labels (`["billing", "tech"]`), an array of
  `{"label", "description"}` objects, or, for `noul` and `choice`, an object of label to description (a description
  may be `null`).
  - `noul` labels are exactly `true` and `false`; labels with no descriptions are the same as no criteria. Pass SQL
    NULL for no criteria.
  - `choice` and `score` take 2 to 255 unique, non-empty labels.
  - `score` levels take no descriptions (describe the scale in `instructions`), and their order is the scale.
- **`questions`** is a JSON object of question id to `{"type", "instructions", "criteria"}`: 1 to 512 questions, ids
  non-empty and unique ignoring case, `type` one of `noul`, `choice` and `score`. A NULL `state` or `questions`
  returns NULL.
- **`images`** is one signed URL or a JSON array of 1 to 8, each a PNG, JPEG or WebP object, from
  `EXTERNAL_OBJECT_TRANSFORM(TABLE <object table>, ['SIGNED_URL'])`. The function reads only
  `https://storage.googleapis.com/` URLs and sends the bytes; Drex never fetches a URL. NULL, an empty array, or a
  NULL in the array returns NULL and sends nothing.
- **`model`**: NULL or `''` means `drex-v2.0`, or `drex-max` for `decide_image`. The name is checked against
  `GET /v1/models`; only image models (`drex-max`, `drex-max-v1.1`) take images.

Every check on the questions, labels and model fails the query, whichever row holds the failing value.

### Images

Create an [object table](https://cloud.google.com/bigquery/docs/object-table-introduction) over your images with the
module's connection, and grant the connection's service account (the module's `connection_service_account` output)
`roles/storage.objectViewer` on the bucket. Then:

```sql
SELECT uri, drex.decide_image(
  JSON '"A delivery photo"',
  TO_JSON(signed_url),
  JSON '{"damaged": {"type": "noul", "instructions": "Is the parcel damaged?"}}',
  NULL
) AS answer
FROM EXTERNAL_OBJECT_TRANSFORM(TABLE photos.objects, ['SIGNED_URL']);
```

For several images in one request, pass `TO_JSON(ARRAY_AGG(signed_url ORDER BY uri))`; the questions see them as
Image 1, Image 2 and so on, in that order. Each request, its images in base64 included, must stay under Drex's 4 MiB,
so an image over about 3 MiB returns NULL.

## Errors

| Outcome | When |
| --- | --- |
| **The query fails** with the function's message | 401 (the key was refused); 402 (no funds, with the billing page); a 422 about the questions or model, with each issue's `path: message`; an unknown model; images on a model that doesn't read them; an image URL that isn't a signed Cloud Storage URL; a TLS failure; `GET /v1/models` unanswered after its retries, or not within the batch's time; any check above |
| **The row is NULL**, with a warning in the function's Cloud Run logs | a 422 about the row alone (`Request too long:`, or issues only under `state` or `images`); a request over 4 MiB; an image that can't be read or isn't a PNG, JPEG or WebP; retries exhausted; any other status |
| **Retried** inside the batch | no response, 408, 429, 500, 502, 503, 504 or 529 |

Treat NULL as an unsuccessful answer, not a negative one. A warning names the row's position in its batch and the
reason, never the row's content or an image URL.

Each request makes at most 5 attempts within 120 s of the first, waiting what the server asks (`retry-after-ms` or
`retry-after`, at most 30 s), else 1, 2, 4 and 8 s with jitter. The function never answers BigQuery with a status
BigQuery retries: a batch runs out of time at the function's timeout less 60 s, and its unanswered rows return NULL.

## Requests and cost

Each non-NULL row sends one request, and only an answered request (HTTP 200) is billed. BigQuery can send a batch it
already got an answer for again ("you might see repeated requests with the same data to your endpoint, even after
successful responses"), and running a query again sends its rows again. Both are billed again. Save answers with
`CREATE TABLE ... AS SELECT` to reuse them. BigQuery never caches the result of a query with a remote function.

## Configuration

Terraform variables of the module:

| Variable | Default | Purpose |
| --- | --- | --- |
| `max_instances` | `1` | Most Cloud Run instances |
| `container_concurrency` | `1` | BigQuery batches one instance answers at once |
| `inflight_per_request` | `8` | Drex requests one batch has in flight |
| `max_batching_rows` | `50` | Rows per batch for `noul`, `choice`, `score` and `decide` |
| `image_max_batching_rows` | `1` | Rows per batch for `decide_image`, as Google recommends for object tables |
| `timeout_seconds` | `300` | The function's timeout; a batch answers within this less 60 s |
| `base_url` | `https://console.nace.ai` | `NACE_BASE_URL`; `https://openrouter.ai/api` with an OpenRouter key |
| `openrouter_api_key_secret` | none | A secret holding an OpenRouter key, set as `OPENROUTER_API_KEY` |
| `openrouter_referer` | `https://nace.ai` | `NACE_OPENROUTER_REFERER`, the app identity OpenRouter attributes requests to |
| `openrouter_title` | `Drex integration for BigQuery` | `NACE_OPENROUTER_TITLE`, the app name OpenRouter shows |

At most `max_instances` x `container_concurrency` x `inflight_per_request` requests are in flight, 8 by default. Keep
it within your account's concurrency limit; above it, Drex answers 429 and the function waits and retries. BigQuery
runs at most 10 queries with remote functions at once per project.

The key is the latest version of the Secret Manager secret `api_key_secret`, read when an instance starts. Running
instances keep the key they started with, and `terraform apply` changes nothing when only the secret changed: after
adding a version, keep the old key valid until the function's instances have been replaced, or deploy a new revision
of its Cloud Run service.

With OpenRouter, the key is `OPENROUTER_API_KEY` when set, else `NACE_API_KEY`; a `nace_` key is refused before any
request. The referer and title headers (`HTTP-Referer`, `X-OpenRouter-Title`) are sent only to OpenRouter, with the
defaults above unless you set `openrouter_referer` and `openrouter_title`. Model names without an owner are sent under
`nace-ai/`, and images go in `state` content parts.
