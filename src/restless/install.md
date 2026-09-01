# install.md: LLM installation reference for restless-sdk (Python)

This file is the single source of truth for LLM agents installing or configuring the Restless Python SDK. Humans should read `README.md` instead.

The document is ordered so an agent can stop as soon as enough context has been loaded: package basics → setup call → framework wiring → redaction → settings file → common mistakes.

---

## 1. What this package is

`restless-sdk` captures HTTP request/response pairs and ships them in batches to the Restless ingest server for dashboard display.

- **Runtime:** Python 3.8+. **No dependencies.**
- **Shape:** one client, two adapters. `client.wsgi(app)` wraps any WSGI app; `client.asgi(app)` wraps any ASGI app.
- **Frameworks supported:** anything speaking WSGI (Flask, Django, Pyramid, Bottle) or ASGI (FastAPI, Starlette, Quart). There is no per-framework import.
- **Import name is not the install name:** you `pip install restless-sdk` and you `import restless`.

## 2. Install

```sh
pip install restless-sdk
```

`poetry add restless-sdk`, `pdm add restless-sdk` and `uv add restless-sdk` work the same. No other packages are required.

## 3. The one-line setup

Construct a client, register a per-request callback, wrap the app:

```python
import os
import restless

client = restless.Restless(os.environ["RESTLESS_KEY"])

@client.setup
def _(request):
    return {
        "api_key": client.mask(request.header("authorization")),
        "owner": {"id": workspace_id_for(request), "enrich": enrich_owner},
    }

app.wsgi_app = client.wsgi(app.wsgi_app)
```

The client exposes four things:

| member          | purpose                                                            |
|-----------------|--------------------------------------------------------------------|
| `setup(cb)`     | Register the per-request callback. Usable as a decorator or a call. |
| `mask(key)`     | Hash an end-user API key for safe logging.                          |
| `wsgi(app)` / `asgi(app)` | Wrap the application object.                              |
| `flush()`       | Force-upload the current batch (e.g. before exit).                  |

`restless.Restless(key)` is the class. `restless.restless(key)` is a functional alias that mirrors the Node SDK's `restless(key)`; they are identical, use whichever reads better.

`mask` is available three ways, all the same function: `client.mask(...)` (a staticmethod), `restless.mask(...)` (module level), and `from restless import mask`.

All examples below use `workspace_id_for(request)` as a placeholder for the customer's stable, immutable internal id. Replace it with whatever your auth layer resolves: a workspace uuid, tenant id, or user pk. See §4.1 for how to pick.

Owner metadata (display label, contact emails, anything else) flows through `owner["enrich"]` — that is the only channel for it, and it is required whenever you set an owner. To keep the per-framework snippets short, they share this resolver:

```python
# Runs once per owner id, then caches. Reuse your project's own data access.
def enrich_owner(owner_id):
    workspace = Workspace.objects.get(pk=owner_id)
    return {"label": workspace.name, "email": workspace.admin_emails}  # str or list[str]
```

### Flask

```python
app = Flask(__name__)
app.wsgi_app = client.wsgi(app.wsgi_app)
```

**Placement:** assign `app.wsgi_app` after creating the app and after any other WSGI middleware you want *inside* the capture. Wrapping outermost is what you want: the SDK then sees the real status and body your app produced rather than an inner layer's.

Route templates come from Flask's `url_rule` automatically, and `/pets/<int:pet_id>` is normalized to `/pets/{pet_id}` so the same endpoint groups identically here and in every other SDK.

### Django

```python
# wsgi.py
from django.core.wsgi import get_wsgi_application
import restless

client = restless.Restless(os.environ["RESTLESS_KEY"])

@client.setup
def _(request):
    return {"api_key": client.mask(request.header("authorization"))}

application = client.wsgi(get_wsgi_application())
```

Wrapping in `wsgi.py` puts capture outside Django's whole middleware stack, which is what you want: authentication has run by the time your handler executes, and the SDK still records the response Django actually sent.

Django exposes no route template to WSGI, so set one yourself if you want grouped routes:

```python
# a Django middleware, installed anywhere in MIDDLEWARE
class RestlessRoute:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        match = getattr(request, "resolver_match", None)
        if match is not None:
            request.META["restless.route"] = "/" + match.route
        return self.get_response(request)
```

Without it, capture still works; `/pets/1` and `/pets/2` just group separately.

### FastAPI / Starlette

```python
app = FastAPI()
app = client.asgi(app)
```

Route templates (`/pets/{pet_id}`) come from the ASGI scope automatically, no extra wiring.

If you prefer Starlette's middleware registration, wrapping the app object as above is still the recommended form: `add_middleware` places the SDK inside the exception middleware, where it cannot see the response for an unhandled error.

### Any other WSGI / ASGI app

```python
application = client.wsgi(application)   # Pyramid, Bottle, bare WSGI
application = client.asgi(application)   # Quart, bare ASGI
```

Set `environ["restless.route"]` (WSGI) or a `route` on the ASGI scope to report route templates from a router the SDK does not know.

## 4. The setup callback

The callback receives a read-only `RequestInfo`, the same object under WSGI and ASGI, so one callback works whichever protocol you are on.

| accessor | what |
|---|---|
| `request.header(name)` | One header, case-insensitive. `request["authorization"]` is an alias. |
| `request.headers` | All of them, as a case-insensitive mapping. |
| `request.method` | `"GET"`, `"POST"`, ... |
| `request.path` | Path, including any mount prefix. |
| `request.query_string` | Raw query string, without the `?`. |
| `request.url` | Full URL, as the capture records it. |
| `request.environ` / `request.scope` | The raw WSGI environ or ASGI scope; the other is `None`. |
| `request.framework_request` | Whatever an upstream layer put in `environ["restless.request"]`, else `None`. |

```python
@client.setup
def _(request):
    return {"api_key": client.mask(request.header("authorization"))}
```

This matches Ruby's `RequestInfo` (`header`, `request_method`, `path`, `query_string`, `url`, raw `env`) and Go's `*RequestInfo` (`Header`, `Request`, `Route`). CONTRACT.md section 14 leaves the callback argument per-language, and parity is the choice this SDK makes.

Nothing is hidden: anything the view does not model is still on `request.environ` / `request.scope`. Under WSGI those are CGI-style keys (`HTTP_AUTHORIZATION`, `PATH_INFO`); under ASGI the scope's `headers` are `(bytes, bytes)` pairs. Prefer the accessors; reach for the raw dict when you need something protocol-specific.

`request.route` is normally `None`. The callback runs before your application, so no router has matched yet, and reporting a route here would be a lie. The captured log still gets the real route, read after the response.

**A callback that raises is silently ignored.** SAFETY-002 requires that observability never breaks the request path, so `resolve()` catches every exception and returns `{}`. That is correct behaviour, and it means a wrong callback does not crash your API, it just quietly attributes nothing. Verify with §16 step 5 rather than assuming.

Result fields:

| field      | type                                  | required | notes                                                             |
|------------|---------------------------------------|----------|-------------------------------------------------------------------|
| `api_key`  | `str \| None`                         | no       | Masked key from `client.mask()`. Never pass plaintext.            |
| `owner`    | `dict`                                | yes\*    | The workspace / tenant / end-user this request belongs to.        |
| `block`    | `True \| {"status":, "message":}`     | no       | Rejects the request with 403 (or custom status). Handler never runs. |

\* `owner` is technically optional, but omitting it lands every log in the dashboard as "anonymous". Set it unless this API has truly no concept of identity.

Extra top-level keys are preserved and stored on the log.

### 4.1 The `owner` block

`owner["id"]` is the **permanent, immutable identifier** the dashboard uses to group every log this customer ever produces. Once set, do not change it: the value is sent on every request and the dashboard pins a project's whole history to it. Misconfiguring this is the single biggest setup mistake.

**Picking `owner["id"]`:**

| API shape                                   | Use as `id`                                         |
|---------------------------------------------|-----------------------------------------------------|
| Multi-tenant SaaS (workspaces, orgs, teams) | The tenant's stable internal id (uuid / pk)         |
| Per-user API (one key per developer)        | The user's stable internal id (uuid / pk)           |
| Anonymous / no identity model               | Omit `owner` entirely; every log lands as anonymous |

**Never use any of these as `owner["id"]`:** an API key (rotatable, also a secret), an email address (changeable), a username, a JWT, a placeholder literal like `"anonymous"` / `"none"` / `"guest"`, or any other value that can change for the same customer. If it can rotate or it's a dummy string, it's wrong.

**For requests with no real owner** (no authenticated user, public endpoints, health checks): omit the `owner` key for that request. Don't substitute a placeholder string: the SDK has its own anonymous bucket on the wire-format side, and a fake `"anonymous"` id fake-groups every unauthenticated request under one tenant on the dashboard.

```python
@client.setup
def _(request):
    result = {"api_key": client.mask(request.header("authorization"))}
    workspace_id = workspace_id_for(request)
    if workspace_id:
        result["owner"] = {"id": workspace_id, "enrich": enrich_owner}
    return result
```

The owner dict:

```python
{
    "id": "wks_01H...",              # permanent, immutable (see above)
    "enrich": lambda owner_id: {     # the ONLY channel for owner metadata
        "label": "Acme Inc",         # optional
        "email": "ops@acme.com",     # optional, str or list[str]
        "plan": "enterprise",        # any extra keys are preserved on the log
    },
}
```

There are no inline `label` / `email` keys on `owner`. Everything except `id` comes back from `enrich`, and anything else set inline is dropped.

### 4.2 What's cheap vs expensive

Top-level `api_key` and `owner["id"]` are **cheap** and included on every request.

`owner["enrich"](id)` is **expensive**. The SDK calls it only on the first request from each `owner["id"]`, then caches until the server asks for a refresh. Its resolved fields are cached and re-attached to every subsequent upload, so each log still carries full owner metadata without re-running the lookup.

Behaviour:

- Cached by `owner["id"]`. First request from each owner triggers `enrich`; subsequent requests skip it.
- If the server responds to an upload with `needsEnrichment: [<owner id>]`, that owner is invalidated and the next request from it re-runs `enrich`.
- `enrich` exceptions are swallowed. The log still ships with the `owner["id"]`.
- `enrich` runs only when `owner["id"]` is set.
- `enrich` is skipped entirely when the same setup result also returns `block`. A blocked request never reaches your handler, so paying for a lookup would mean a banned tenant costing one database round-trip per owner id, forever. The `owner["id"]` still ships.

## 5. The `mask()` gotcha

`client.mask(value)` produces `sha512-<base64>?<last4>`. The suffix is the LAST 4 CHARACTERS OF THE INPUT, which means substituting a placeholder leaks info.

```python
# CORRECT: None when the header is missing
"api_key": client.mask(request.header("authorization"))

# WRONG: the fallback string gets hashed and "mous" ends up as the last4
"api_key": client.mask(request.header("authorization") or "anonymous")
```

`mask()` returns `None` on falsy input. The SDK handles it. Don't substitute.

## 6. `.restless/settings.json`

The SDK auto-reads this file at startup (walking up from the working directory). Created and owned by the `restless` CLI (`npx restless init`). Every Restless SDK reads the same file with the same camelCase keys, so a polyglot repo needs only one.

```json
{
  "version": 1,
  "apis": [
    {
      "id": "<api uuid>",
      "name": "Public API",
      "rootDir": ".",
      "projectId": "<restless project uuid>",
      "oasFile": ".restless/openapi.yaml",
      "framework": "flask",
      "language": "python",
      "baseUrl": "https://api.example.com",
      "internal": false,
      "requestIdPrefix": "PUB",
      "redact": {
        "headers":     ["x-company-auth"],
        "queryParams": ["signed_token"],
        "bodyKeys":    ["ssh_private_key"]
      }
    }
  ]
}
```

What the SDK reads from each `apis[]` entry:

- `requestIdPrefix` → prepended to the UUID in response headers (decorative)
- `redact` → merged with built-in redaction defaults

(Other fields, `projectId` included, are consumed by the `restless` CLI during setup, not the SDK at runtime. `projectId` is per-API and lives on the entry; there is no top-level `projectId`.)

If multiple APIs are defined, pick one:

```python
restless.Restless(os.environ["RESTLESS_KEY"], api="Public API")
```

If exactly one is defined, it is used automatically. Zero = no auto-config.

## 7. Redaction (on by default)

Sensitive values are redacted BEFORE anything leaves the process.

### Built-in denylists (always applied)

- **Headers:** `authorization`, `cookie`, `set-cookie`, `proxy-authorization`, `x-api-key`, `x-auth-token`
- **JSON body keys:** `password`, `pass`, `pwd`, `token`, `secret`, `apikey`, `accesstoken`, `refreshtoken`, `idtoken`, `sessionid`, `ssn`, `creditcard`, `ccnumber`, `cvv`, `cvc`
- **Query params:** same list as body keys

Matching is case-insensitive AND ignores `-`/`_`, so `api_key` / `apiKey` / `API-KEY` / `APIKEY` all match.

For `authorization` and `proxy-authorization` the auth-scheme word survives (`Bearer <REDACTED:...>`), so a debugger can tell Bearer from Basic at a glance while the credential is still gone.

### Extending

Two additive sources, both merged with the defaults:

1. **`.restless/settings.json` → `apis[].redact`** (populated by `npx restless init`)
2. **the `redact` argument**, per-process:
   ```python
   restless.Restless(key, redact={"headers": ["x-custom"], "bodyKeys": ["apiSecret"]})
   ```

Note the argument keys are camelCase (`bodyKeys`, `queryParams`), matching the settings file and the other SDKs rather than Python convention. That is deliberate: it is the same wire vocabulary everywhere.

### Sentinel format (stable contract)

```
<REDACTED:<length>>                 # when length < 8
<REDACTED:<length>:<last-4-chars>>  # when length >= 8
```

Regex: `<REDACTED:(\d+)(?::(.{4}))?>`. The dashboard pattern-matches on this.

### Body size limit

Captured bodies are capped at **256 KiB** (UTF-8 bytes). Larger bodies are truncated with `[...TRUNCATED: original N bytes]`. Not configurable.

## 8. Request IDs

- Always v4 UUIDs. NOT time-based, so they leak no ordering or timing.
- **Exactly one** id header per response, always carrying our own freshly-minted id.
- **Default: `x-request-id`.** A plain `curl` of your API comes back with that one.
- **`x-restless-id` only when the incoming request already carried an `x-request-id`** - we answer on our own header rather than stomping an existing request-id chain.
- Incoming `x-request-id` values are NEVER reused as our ID.
- When no `RESTLESS_KEY` resolves, the value is the literal string `missing-key` instead of a UUID - the signature of a server running without the key.

## 9. Response modification (SDK-owned, not configurable)

The SDK injects debug info:

- Response headers, on **every** status: `x-log-url: <portalOrigin>/logs/<id>`, `x-debug: npx api debug <id>`
- Response body, only on status **>= 400** and only when `content-type: application/json`: a `debug` key merged into the top-level object, carrying `log`, `cli` and `recovery`.

`<portalOrigin>` is your project's public docs host, which the server tells the SDK on each upload. Until the first upload round-trips, `x-log-url` is omitted rather than guessed: a URL that 404s is worse than no URL. The ingest host is never used for it.

There is no user-configurable body or header hook. Don't look for one.

## 10. Blocking

```python
@client.setup
def _(request):
    if is_banned(request):
        return {"block": True}                                    # 403 Forbidden
    if rate_limited(request):
        return {"block": {"status": 429, "message": "slow down"}}
    return {"api_key": client.mask(request.header("authorization"))}
```

The handler never runs for blocked requests. The response body is JSON `{"error": "<message>"}`. `owner["enrich"]` is not called for a blocked request (§4.2).

## 11. Uncaught handler errors

A handler that raises produces a log like any other request, grouped by the exception's raise site (file + function, never line numbers) rather than by the wording of the error your framework rendered. That keeps one bug in one dashboard group even when its message interpolates a different id each time.

The exception is always re-raised untouched, so your framework's error handling behaves exactly as it did without the SDK.

Because both adapters wrap the *whole* application, this needs no extra wiring in any framework. There is no Python equivalent of the Node SDK's Express-only `errorHandler`.

## 12. Environment variables

| variable            | effect                                                                       |
|---------------------|------------------------------------------------------------------------------|
| `RESTLESS_KEY`      | Fallback API key when `Restless()` is called without one                     |
| `README_API_KEY`    | Secondary fallback (checked after `RESTLESS_KEY`)                            |
| `RESTLESS_BASE_URL` | Override the ingest URL. Beaten by the `base_url` argument. **Non-localhost `http://` triggers a loud stderr warning** (plaintext auth). |
| `DEBUG=restless`    | Print upload errors / queue warnings to stderr                               |
| `RESTLESS_ENV=test` | Force test-run mode (see §13)                                                |

The SDK does NOT auto-load `.env` files. Python has no single convention there, so loading is left to your project: `python-dotenv`, `django-environ`, your process manager, or your deployment platform. Whatever populates `os.environ` before the client is constructed works.

## 13. Batching

Hardcoded behaviour, no config:

- Batch size: 10 captured requests
- Flush interval: 5000 ms
- Queue cap: 1000 entries (drops oldest on overflow)
- Localhost base URL: flushes every push (instant dev feedback)

Uploads run on a background thread and never block a response. Failures are swallowed; with `DEBUG=restless` they print to stderr.

**Test runs do not upload.** Detected via `RESTLESS_ENV=test`, `PYTEST_CURRENT_TEST`, `PYTEST_VERSION`, `pytest` / `unittest` in `sys.modules`, or `NOSE_config`. Capture still runs, so tests exercise the real path.

## 14. Options reference

```python
restless.Restless(
    api_key=None,     # falls back to RESTLESS_KEY, then README_API_KEY
    api=None,         # name of the API in .restless/settings.json; required when >1
    redact=None,      # {"headers": [...], "bodyKeys": [...], "queryParams": [...]}
    base_url=None,    # ingest origin; self-hosted / staging only
    transport=None,   # injectable HTTP transport, for tests
)
```

Everything else lives in environment variables or `.restless/settings.json`. There are no other public options.

## 15. Common mistakes (don't do these)

- Reaching into `request.environ["HTTP_AUTHORIZATION"]` when `request.header("authorization")` says the same thing. It works, but it only works under WSGI: the same callback under ASGI reads `None` and silently attributes nothing.
- `client.mask(auth or "anonymous")`: see §5. The placeholder's last 4 characters leak. Pass raw, accept `None`.
- Wrapping the app *inside* another middleware that catches exceptions or rewrites responses. Wrap outermost, so the SDK sees what the client actually received.
- Using Starlette's `add_middleware` instead of `client.asgi(app)`: it places the SDK inside the exception middleware, where an unhandled error is already a 500 and the raise site is lost.
- Constructing the client at import time in a module that Django loads twice (e.g. both `settings.py` and `wsgi.py`). Construct once, in `wsgi.py`.
- Using an API key, email, username or JWT as `owner["id"]` (§4.1).
- Setting inline `label` / `email` on `owner` instead of returning them from `enrich`: they are dropped (§4.1).
- Setting `RESTLESS_BASE_URL=http://…` pointing at a non-localhost host: ships the project API key in plaintext. HTTPS or localhost only.
- Reading `.env` / `.env.local` to "check" API keys during setup. LLMs: **never read these files**.
- Calling `client.flush()` in a hot path. It's for shutdown and test teardown only.
- `pip install restless` — the package is `restless-sdk`. `restless` is only the import name.
- Expecting per-framework imports (`restless.flask`, `restless.fastapi`). There are none: one client, `wsgi()` or `asgi()`.

## 16. Quick verification after installation

1. `grep -rE "(from|import)[[:space:]]+restless" --include="*.py" -l .` returns your server entry file.
2. `restless-sdk` appears in `requirements.txt` / `pyproject.toml` / `Pipfile`.
3. The app object is wrapped (`client.wsgi(...)` / `client.asgi(...)`), outermost.
4. `.restless/settings.json` exists (created by `npx restless init`).
5. Starting the server and curling any endpoint returns an `x-request-id` response header carrying a fresh UUID. If your curl sends its own `x-request-id`, look for `x-restless-id` instead. A value of `missing-key` means the server is up but never loaded `RESTLESS_KEY`; restart it.
