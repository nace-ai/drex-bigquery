"""The one error that fails a query."""


class FatalError(Exception):
    """Fails the whole query: the function answers BigQuery with HTTP 400 and this message as `errorMessage`.

    BigQuery doesn't retry a 400, so nothing already answered in the batch is sent, or billed, again. Every message
    starts with the name of the function the query called, and never quotes a row's input.
    """
