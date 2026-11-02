# Changelog

## 0.1.0

The first release.

- Five BigQuery remote functions, `noul`, `choice`, `score`, `decide` and `decide_image`, answered by a Cloud Run
  function that calls Drex through `nace-sdk` 0.6.2, or OpenRouter.
- A Terraform module that deploys the function, the BigQuery connection, the dataset and its routines, reading the API
  key from Secret Manager so it never enters Terraform state.
- Images from Cloud Storage object tables for `decide_image`, through signed URLs.
- Query-failing checks for questions, labels, models and image URLs; NULL with a logged warning for rows Drex can't
  answer; retries with a per-request budget; a batch deadline under the function's timeout.
