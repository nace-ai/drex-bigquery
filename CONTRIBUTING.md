# Contributing

Use [GitHub Issues](https://github.com/nace-ai/drex-bigquery/issues) for bugs and proposals. Include the integration
version, runtime version, expected behavior and a small reproduction with synthetic inputs. Never include credentials,
signed image URLs or customer data.

## Local checks

Clone the repository and follow [Development](README.md#development) from the repository root.
The tests run the function against a local mock of the Drex API; a passing mock test is not a live check against
BigQuery. See [.github/workflows](.github/workflows) for the CI commands. Describe any checks you skipped in the
pull request.

## Pull requests

Keep the change focused, explain the behavior it changes and list the checks you actually ran. Update the
[API reference](docs/API.md) and examples if an interface changes. Preserve NULL/error semantics, credential
handling and request-cost warnings. Use synthetic fixtures and avoid committing generated bundles or local secrets.

Don't add or change names, logos or other artwork without the maintainers' agreement: an official download is not,
by itself, permission to use a provider's logo.
