"""Worker entry point.

`asgi.entrypoint` wires the Workers runtime's built-in ASGI server to the
FastAPI app, so requests arrive as ASGI scopes with the bindings exposed at
`scope["env"]`.
"""

from workers import asgi

from bauhaus_api.app import app

Default = asgi.entrypoint(app)

__all__ = ["Default"]
