"""Shared test helpers.

Importable because conftest.py puts this directory on sys.path.
"""

from unittest.mock import MagicMock

from botocore.exceptions import ClientError


def make_r2_client(published: bool = False) -> MagicMock:
    """A mock S3 client whose head_object reports the date as un/published.

    upload() refuses to overwrite an already-published date, so head_object has
    to answer that question. A bare MagicMock would return a truthy object and
    make every date look published; this makes "nothing there yet" the default,
    which is what a normal daily run sees.
    """
    client = MagicMock()
    if not published:
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"},
             "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )
    return client
