"""Reading `drex.decide_image`'s images from the signed URLs of an object table.

Drex never fetches a URL, so the function downloads each object and sends its bytes. It downloads only from
`https://storage.googleapis.com/`, the host `EXTERNAL_OBJECT_TRANSFORM(..., ['SIGNED_URL'])` signs for, so a query
can't steer it at another host or an internal address. A signed URL is a credential: it is never logged or quoted.
"""

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx

from drex_bigquery.drex import MAX_BODY_BYTES, REQUEST_TIMEOUT_SECONDS, Lost, Response, with_retries
from drex_bigquery.errors import FatalError

ALLOWED_HOST = "storage.googleapis.com"
#: Drex takes at most this many images in one request
MAX_IMAGES = 8
#: An image can't be larger than the whole request body it is sent in
MAX_IMAGE_BYTES = MAX_BODY_BYTES


def image_urls(function: str, value: Any) -> list[str] | None:
    """The URLs of the `images` argument: one URL or a JSON array of them. None when the row sends nothing: no images,
    or a NULL among them."""
    urls = value if isinstance(value, list) else [value]
    if not urls or any(url is None for url in urls):
        return None
    for url in urls:
        if not isinstance(url, str):
            raise FatalError(f"{function}: images must be a signed URL or a JSON array of signed URLs")
        parts = urlsplit(url)
        try:
            httpx.URL(url)  # what the download will send, which refuses some URLs urlsplit takes
        except httpx.InvalidURL:
            parts = None
        if (
            parts is None
            or parts.scheme != "https"
            or parts.hostname != ALLOWED_HOST
            or parts.port not in (None, 443)
            or parts.username
        ):
            raise FatalError(
                f"{function}: images must be signed URLs on https://{ALLOWED_HOST}/, as "
                "EXTERNAL_OBJECT_TRANSFORM(TABLE <object table>, ['SIGNED_URL']) makes them"
            )
    return urls


def _never_fatal(_: Response) -> None:
    pass


async def download(http: httpx.AsyncClient, url: str, number: int, slots: asyncio.Semaphore) -> bytes | Lost:
    """Image `number`'s bytes, or why it couldn't be read. Retried as a Drex request is."""
    content = b""
    too_large = False

    async def send() -> Response:
        nonlocal content, too_large
        try:
            # No redirects: the host check above holds only for this URL
            async with http.stream("GET", url, follow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status_code != 200:
                    return Response(response.status_code, headers=response.headers)
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        too_large = True
                        return Response(200)
                    chunks.append(chunk)
                content = b"".join(chunks)
                return Response(200)
        except httpx.HTTPError as error:
            # The error's text can quote the URL
            return Response(0, transport_error=type(error).__name__)

    result = await with_retries(send, slots, _never_fatal)
    if isinstance(result, Lost):
        return Lost(f"could not read image {number}: {result.reason}")
    if too_large:
        return Lost(f"image {number} is over {MAX_IMAGE_BYTES} bytes, more than a Drex request can hold")
    return content
