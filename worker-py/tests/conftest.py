from __future__ import annotations

import pytest
from fakes import FakeBucket, FakeEnv, bucket_with_day
from httpx import ASGITransport, AsyncClient

from bauhaus_api.app import app


class EnvMiddleware:
    """Puts the bindings on the ASGI scope, as the Workers ASGI server does."""

    def __init__(self, inner, env):
        self.inner = inner
        self.env = env

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "env": self.env}
        await self.inner(scope, receive, send)


def make_client(env: FakeEnv) -> AsyncClient:
    transport = ASGITransport(app=EnvMiddleware(app, env))
    return AsyncClient(transport=transport, base_url="https://api.test")


@pytest.fixture
def env() -> FakeEnv:
    return FakeEnv(BUCKET=bucket_with_day())


@pytest.fixture
def empty_env() -> FakeEnv:
    return FakeEnv(BUCKET=FakeBucket())


@pytest.fixture
async def client(env: FakeEnv):
    async with make_client(env) as c:
        yield c
