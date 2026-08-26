<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/restless-init-dark.svg">
  <img width="100%" src="docs/restless-init.svg" alt="Restless">
</picture>

Run in your codebase to get started:

```sh
npx restless init
```

This scans your project, figures out your framework, generates an OpenAPI spec, automatically wires the SDK into your server.

# restless-sdk

Capture your API traffic and send it to [Restless](https://restless.ai).

Works with any **WSGI** app (Flask, Django, Pyramid, Bottle) or **ASGI** app
(FastAPI, Starlette, Quart). Python 3.8+. No dependencies.

## Install

```sh
pip install restless-sdk
```

## Use

```python
import os
import restless

client = restless.Restless(os.environ["RESTLESS_KEY"])

@client.setup
def _(request):
    return {
        "api_key": client.mask(request.headers.get("authorization")),
        "owner": {
            # Permanent and immutable. A workspace id or database primary
            # key, never an API key, email, or anything else that rotates:
            # the dashboard pins a project's whole log history to this.
            "id": workspace_id_for(request),
            # Runs once per owner id, then caches. Put the expensive lookup
            # here, not in the fields above.
            "enrich": lambda owner_id: {
                "label": db.workspaces.get(owner_id).name,
                "email": db.workspaces.get(owner_id).admin_emails,
            },
        },
    }
```

Then wrap your app:

```python
# Flask / Django / any WSGI
app.wsgi_app = client.wsgi(app.wsgi_app)

# FastAPI / Starlette / any ASGI
app = client.asgi(app)
```

## The request view

The callback receives a read-only `RequestInfo`, identical under WSGI and
ASGI, so the same callback works whichever you are on.

| accessor | what |
|---|---|
| `request.header(name)` | One header, case-insensitive. `request["authorization"]` is an alias. |
| `request.headers` | All of them, as a case-insensitive mapping. |
| `request.method` | `"GET"`, `"POST"`, ... |
| `request.path` | Path, including any mount prefix. |
| `request.query_string` | Raw query string, without the `?`. |
| `request.url` | Full URL, as the capture records it. |
| `request.environ` / `request.scope` | The raw WSGI environ or ASGI scope; the other is `None`. |

Nothing is hidden by the wrapper: anything it does not model is still on
`request.environ` / `request.scope`.

`request.route` is normally `None` here. The callback runs before your
application, so no router has matched yet. The captured log still gets the
real route, read after the response.

## What you get

- **Lazy owner enrichment.** The `enrich` callback runs on the first request
  from each owner id and then caches, so 100 requests from one workspace do
  not mean 100 database lookups.
- **Safe by default.** `Authorization`, `Cookie`, `password`, `token`, `ssn`
  and friends are redacted before anything leaves your process. Bodies with
  nothing to redact are passed through untouched, so your payloads are not
  reserialized on the way out.
- **Error triage.** Every response gets `x-log-url` and `x-debug` headers,
  and 4xx/5xx responses also get a `debug` block in the JSON body. If someone attaches a "next steps"
  message to an error in the dashboard, the SDK injects it as
  `debug.recovery`, read synchronously from an in-process cache, never
  blocking the response on a network call.
- **Blocking.** Return `{"block": True}` from the setup callback to reject a
  request before your handler runs.

## Never breaks your API

Observability must not take down a production request path. Upload failures,
callback exceptions, unserializable bodies and malformed input are all caught
and swallowed (surfaced only under `DEBUG=restless`). Uploads happen on a
background thread.

## Environment variables

| variable | purpose |
|---|---|
| `RESTLESS_KEY` | Your project API key, if you do not pass one explicitly. |
| `RESTLESS_BASE_URL` | Override the ingest URL (self-hosted or staging). |
| `DEBUG=restless` | Print upload diagnostics to stderr. |

## Conformance

This SDK implements version 1.0.0 of the Restless SDK Contract at level L2,
and is verified against the shared cross-language conformance vectors. See
[CONFORMANCE.md](./CONFORMANCE.md).

## License
MIT
