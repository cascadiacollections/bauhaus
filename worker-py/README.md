# Bauhaus API on FastAPI (Cloudflare Python Workers)

A working port of `worker/src/index.ts` to FastAPI, running on [Cloudflare Python
Workers](https://developers.cloudflare.com/workers/languages/python/).

It deploys as `bauhaus-py`, a **separate Worker on its own hostname**. The
TypeScript worker in `../worker` still serves production; this one is wired up
alongside it so it can be exercised on real infrastructure — which is the only
way to answer the cold-start question below — without putting traffic on it.

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
    tools/parity.py               Differential check against the deployed TypeScript worker
    tests/                        170 tests, runnable under plain CPython

`logic.py` is deliberately free of runtime imports. It holds the parts with real
edge cases — negotiation, ETag comparison, archive paging, telemetry shaping —
so they can be tested with an ordinary `pytest` run rather than only through a
dev server. `tests/fakes.py` fakes the R2 and Analytics Engine bindings, so the
FastAPI app is also exercised end-to-end without `workerd`.

## Running it

```bash
cd worker-py
uv sync --locked
uv run pytest              # 170 tests, ~0.3s
uv run pywrangler dev      # local workerd + Pyodide
```

## Parity with the TypeScript worker

Each implementation is tested against its own expectations, so two suites
passing only shows that two readings of the same spec agree with themselves.
`tools/parity.py` sends one matrix of ~60 requests to **both deployed Workers**
and diffs status, the headers that matter to clients (`Cache-Control`, `Vary`,
`ETag`, `X-Variant`, `Content-Type`, `Accept-CH`, CORS) and the response bodies:

```bash
just worker-py-parity                       # both defaults, as CI runs it
just worker-py-parity -- --candidate http://localhost:8787
```

Both Workers read the same bucket, so the ETag, the stored `Cache-Control` and
the image bytes all come from the same R2 object — a difference is the serving
layer's, not the data's. The matrix covers format negotiation, `?progressive=`
and `?strip=`, `If-None-Match` (resolved per host, so neither side is handed the
other's validator), HEAD, archive paging and its query validation, the
never-published 404 path, routing edges, and the telemetry CORS rejections.
Nothing in it writes: the `POST` cases all carry an Origin the Worker refuses,
so they return before `writeDataPoint` and leave no rows in Analytics Engine.

`/api/today*` cases are retried once before being reported, so a publish landing
mid-run is not mistaken for a regression.

It runs daily at 05:30 UTC and after every `bauhaus-py` deploy
(`.github/workflows/parity.yml`). A green run is the cutover gate.

## Cutting over

The hostname is the constraint, not the runtime. `bauhaus-android` compiles
`https://bauhaus.cascadiacollections.workers.dev` into shipped APKs in two
places — `BauhausApi.BASE_URL` and, separately, `HttpModule.CDN_HOST`, which
gates the `Accept`-injection interceptor that keeps Coil and the API on one
cache key. A `workers.dev` subdomain is derived from the Worker *name*, so
"point traffic at `bauhaus-py`" is a URL change that installed clients can never
follow.

Cutover therefore means deploying this code **under the existing `bauhaus`
Worker name**, not switching hostnames — ideally as a gradual deployment
(1% → 10% → 100%), where rollback is a version pin rather than a redeploy.

## CI and deployment

- **`Worker (Python port)`** in `.github/workflows/ci.yml` runs `uv sync
  --locked`, the test suite, and `pip-audit` against this project's resolved
  dependencies. The root Python jobs resolve the root `pyproject.toml` and do
  not cover this tree; `ruff` is the one check shared with them, since the root
  lint job lints the whole repository.
- **`.github/workflows/deploy-worker-py.yml`** deploys `bauhaus-py` on pushes to
  `main` that touch `worker-py/`, and on demand. It is a separate workflow from
  `deploy.yml` so that a failure here cannot block or destabilise the production
  TypeScript deploy. Its post-deploy health check proves the new build answers;
  the parity job it then calls proves it answers *the same*.
- **`.github/workflows/parity.yml`** runs the differential check daily and on
  demand, and is what the deploy workflow calls. It cannot run on a pull
  request: it needs two deployed Workers.

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

The port is complete, tested in CI, deployable, and continuously diffed against
the implementation it would replace. The open questions are operational, not
technical, and are why production still points at TypeScript:

- **Beta runtime.** Python Workers still require the `python_workers`
  compatibility flag and are documented as beta. That is the main argument for
  leaving the TypeScript worker serving production.
- **Cold starts.** Still the deciding number, and it cannot be measured locally.
  Now that `bauhaus-py` deploys, it can be measured against the TypeScript
  worker on real infrastructure.
- **Body buffering.** Acceptable at these image sizes, but it is a step down
  from streaming and would matter if the artwork got much larger.

What it buys: one language across the repo, so the pipeline in `src/` and the
API stop describing the same `latest.json` and manifest shapes in two languages,
and a test suite that runs in 0.2s under plain pytest instead of via Vitest and
a Wrangler harness.
