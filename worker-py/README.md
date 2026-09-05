# Bauhaus API on FastAPI (Cloudflare Python Workers)

A working port of `worker/src/index.ts` to FastAPI, running on [Cloudflare Python
Workers](https://developers.cloudflare.com/workers/languages/python/). This is a
**prototype for evaluation**, not a replacement — the TypeScript worker in
`../worker` remains the deployed API.

The point of doing it on Python Workers rather than a self-hosted target is that
nothing about the deployment model changes: same edge, same CDN caching, same
free tier, and the same native `R2` and Analytics Engine bindings. Only the
language changes.

## What it does

Every route, header, status code and error body from the TypeScript worker,
verified against a local `workerd` instance:

| Route | Notes |
| --- | --- |
| `GET\|HEAD /api/today` | negotiated image, `TODAY_CACHE` |
| `GET\|HEAD /api/today.json` | metadata |
| `GET\|HEAD /api/today.manifest.json` | responsive manifest |
| `GET\|HEAD /api/:date` | stylized image, immutable cache |
| `GET\|HEAD /api/:date/original` | original unstylized image |
| `GET\|HEAD /api/:date.json` | metadata |
| `GET\|HEAD /api/:date.json.sig` | detached PGP signature |
| `GET\|HEAD /api/:date.manifest.json` | responsive manifest |
| `GET\|HEAD /api/archive` | published dates, paged by `?before=` |
| `GET /api/health` | publish freshness |
| `POST /api/vitals`, `POST /api/err` | RUM ingest to Analytics Engine |

Format negotiation (`?format=`, `Accept`), `?progressive=`, `?strip=`, ETag /
`If-None-Match` / 304, `head()`-before-`get()` so a 304 or a `HEAD` never reads
an object body, and origin-gated telemetry CORS all behave as before.

## Layout

    src/entry.py                  Worker entry point (ASGI ↔ workerd)
    src/bauhaus_api/app.py        FastAPI routes and R2 access
    src/bauhaus_api/logic.py      Pure logic — stdlib only, no FastAPI or Workers imports
    tests/                        134 tests, runnable under plain CPython

`logic.py` is deliberately free of runtime imports. It holds the parts with real
edge cases — negotiation, ETag comparison, archive paging, telemetry shaping —
so they can be tested with an ordinary `pytest` run rather than only through a
dev server. `tests/fakes.py` fakes the R2 and Analytics Engine bindings, so the
FastAPI app is also exercised end-to-end without `workerd`.

## Running it

```bash
cd worker-py
uv sync
uv run pytest              # 134 tests, ~0.2s
uv run pywrangler dev      # local workerd + Pyodide
```

> **No lockfile is committed yet.** It was generated here behind a private
> package mirror, so `uv.lock` and `pylock.toml` came out pinned to internal
> feed URLs that would not resolve for anyone else. Run `uv lock` on a machine
> with public PyPI access and commit the result.

Seed the local R2 bucket to exercise the image routes:

```bash
DATE=$(date -u +%F); P=$(echo $DATE | tr '-' '/')
echo "{\"date\":\"$DATE\"}" > /tmp/latest.json
npx wrangler r2 object put --local bauhaus/latest.json --file /tmp/latest.json
npx wrangler r2 object put --local bauhaus/stylized/$P.jpg --file some.jpg
```

### Node version

`pywrangler dev` builds a Pyodide virtualenv and needs Node's
`--experimental-wasm-stack-switching` flag, which **Node 26 removed**. Use the
Node 24 already pinned in `.mise.toml`:

```bash
PATH="$HOME/.local/share/mise/installs/node/24/bin:$PATH" uv run pywrangler dev
```

## How it differs from the TypeScript worker

Three real differences, none of them behavioural:

1. **Bodies are buffered.** The TypeScript worker passes R2's `ReadableStream`
   straight to the `Response` and never touches the bytes. An ASGI app has to
   materialize the body, so each image response allocates a few hundred KB. That
   is well inside the Worker memory limit but it is a genuine efficiency
   regression, and the reason `head()` is used before `get()` matters more here
   than it did there.
2. **The candidate-key fallback chain is written once.** `getImageObject` and
   `headImageObject` in the TypeScript worker spell out the same
   progressive → stripped → negotiated → JPEG order twice, and can drift.
   `candidate_keys()` returns it as an ordered list that both paths consume.
3. **Routing is declarative.** The chain of `path.match(/regex/)` becomes
   FastAPI route decorators. Registration order is load bearing — the literal
   `/api/today*` and `/api/:date.json.sig` routes must precede the
   `/api/{date}.json` and `/api/{date}` patterns that would otherwise swallow
   them — and a non-date `{date}` segment falls through to the same catch-all
   404 rather than becoming a 422.

## Measurements

Warm request latency under local `workerd`, `GET /api/today`: **~5ms** average
over 5 requests. Cold start was not measured; Cloudflare's memory-snapshot work
is the relevant variable and needs measuring on deployed infrastructure, not
locally.

## Verdict

The port is complete and works. The open questions are operational, not
technical:

- **Beta runtime.** Python Workers still require the `python_workers`
  compatibility flag and are documented as beta. That is the main argument for
  leaving the TypeScript worker deployed.
- **Cold starts.** Needs a real measurement on deployed infrastructure against
  the TypeScript worker before this could replace it.
- **Body buffering.** Acceptable at these image sizes, but it is a step down
  from streaming and would matter if the artwork got much larger.

What it buys: one language across the repo, so the pipeline in `src/` and the
API stop describing the same `latest.json` and manifest shapes in two languages,
and a test suite that runs in 0.2s under plain pytest instead of via Vitest and
a Wrangler harness.
