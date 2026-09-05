<div align="center">
  <a href="https://github.com/dhaval-vedra/VEDRAKIT">
    <img src="assets/vedrakit-banner.svg" alt="Vedrakit — dependency-light Python web framework" width="100%">
  </a>
  <p><strong>Build small. Ship clear.</strong></p>
  <p>
    <a href="https://github.com/dhaval-vedra/VEDRAKIT">Repository</a>
    · <a href="docs/API.md">Complete API reference</a>
    · <a href="examples/basic_app.py">Runnable example</a>
  </p>
</div>

# Vedrakit

Vedrakit is a small, dependency-light Python web framework for building HTTP
APIs without hiding the runtime behind a large stack. It started as a
FastAPI-like prototype and is now organized as a reusable package with
production-oriented defaults, isolated `App` instances, automated tests, and
optional integrations.

> **Documentation:** This README is the product overview and quick-start guide.
> For the complete public API, signatures, behavior tables, and integration
> examples, read [docs/API.md](docs/API.md).

<div align="center">
  <sub>Animated SVG banner · standard-library core · SQLite + PostgreSQL · OpenAPI · JWT · metrics</sub>
</div>

The framework keeps the original prototype's feature surface:

- Decorator-based routing: `get`, `post`, `put`, `delete`, and `route`
- Dynamic path parameters, query parameters, JSON bodies, and type coercion
- `BaseModel` request/response validation
- SQLite models and migrations
- Password hashing, JWT authentication, and role-based access control
- Middleware, exception handlers, dependency injection, and background tasks
- Redis-backed cache/rate limiting with a safe in-memory fallback
- Static files with path traversal protection
- OpenAPI JSON at `/docs`
- Health and readiness endpoints
- Prometheus-compatible metrics at `/metrics`
- Optional WebSocket, GraphQL, and gRPC support

## Requirements

- Python 3.10 or newer
- No runtime dependency is required for the core HTTP, validation, SQLite,
  authentication, queue, docs, or metrics features

Optional integrations can be installed separately:

```bash
pip install -e ".[redis]"
pip install -e ".[graphql]"
pip install -e ".[websocket]"
pip install -e ".[grpc]"
pip install -e ".[postgresql]"
# or everything:
pip install -e ".[all]"
```

For local development and the included tests:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

## Quick start

```python
from vedrakit import App, BaseModel, Query


class UserCreate(BaseModel):
    username: str
    age: int


app = App()


@app.route("/users", ["POST"])
def create_user(user: UserCreate):
    return {"username": user.username, "age": user.age}


@app.route("/users", ["GET"])
def list_users(limit: int = Query(default=20)):
    return {"items": [], "limit": limit}


if __name__ == "__main__":
    app.run(port=8080)
```

Run the complete example:

```bash
python -m examples.basic_app
```

Then visit:

- `GET /health` — liveness
- `GET /ready` — database readiness
- `GET /docs` — generated OpenAPI 3.0.3 document
- `GET /metrics` — Prometheus-compatible metrics

## Application objects and decorators

`App` is recommended for production applications and tests because each
instance has its own routes and middleware:

```python
from vedrakit import App

app = App(static_dir="static")


@app.route("/users/{user_id}", ["GET"])
def get_user(user_id: int):
    return {"id": user_id}
```

The original global decorator API remains available:

```python
from vedrakit import get, run


@get("/health-check")
def health_check():
    return {"ok": True}


run()
```

A route can return a dictionary, a string, or `(status_code, content)`:

```python
@app.route("/created", ["POST"])
def created():
    return 201, {"created": True}
```

Supported HTTP methods are `GET`, `POST`, `PUT`, `PATCH`, and `DELETE`.
Unsupported methods receive `405 Method Not Allowed` with an `Allow` header.
Unknown paths receive `404`.

## Request and response models

`BaseModel` validates annotated fields and converts common primitive types.
Fields without a class default are required:

```python
class SearchRequest(BaseModel):
    phrase: str
    page: int
    include_archived: bool = False


@app.route("/search", ["POST"])
def search(request: SearchRequest):
    return {
        "phrase": request.phrase,
        "page": request.page,
        "include_archived": request.include_archived,
    }
```

JSON requests must use `Content-Type: application/json`:

```bash
curl -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{"phrase":"books","page":1,"include_archived":false}'
```

Use `response_model` to validate successful responses:

```python
class UserResponse(BaseModel):
    id: int
    username: str


@app.route("/users/{user_id}", ["GET"], response_model=UserResponse)
def user(user_id: int):
    return {"id": user_id, "username": "Ada"}
```

Use `Query` for documented, optional query parameters:

```python
@app.route("/items", ["GET"])
def items(skip: int = Query(default=0), limit: int = Query(default=20)):
    return {"skip": skip, "limit": limit}
```

## Middleware, exceptions, and dependencies

Middleware runs before routing. Return `False` to stop a request with `403`:

```python
@app.middleware
def require_internal_header(request):
    return request.headers.get("X-Internal") == "yes"
```

Custom exception handlers are registered by exception type:

```python
@app.exception_handler(ValueError)
def bad_request(error):
    return 422, {"error": str(error), "type": "validation_error"}
```

Dependencies can use the current request and inject their result into the
handler:

```python
from vedrakit import depends


def request_id(req):
    return req.headers.get("X-Request-ID", "generated-locally")


@app.route("/request-info", ["GET"])
@depends(request_id)
def request_info(request_id):
    return {"request_id": request_id}
```

Dependencies can also be used for authentication; the framework makes the
authenticated payload available as `req.request_context["user"]`.

## Authentication and RBAC

Vedrakit provides:

- PBKDF2-HMAC-SHA256 password hashes with a per-password random salt
- Constant-time password verification
- HS256 JWT issuance and verification
- Expiration (`exp`) and issued-at (`iat`) claims
- Route-level authentication
- Role checks for `admin`, `user`, `guest`, and `moderator`

```python
from vedrakit import Role, Security

password_hash = Security.hash_password("a long password")
assert Security.verify_password("a long password", password_hash)

token = Security.create_jwt({"user_id": 123, "role": "admin"})


@app.route(
    "/admin",
    ["GET"],
    require_auth=True,
    required_roles=[Role.ADMIN],
)
def admin_area():
    return {"access": "granted"}
```

Send the token as:

```text
Authorization: Bearer <token>
```

Older prototype SHA-256 password hashes can still be verified so existing
accounts can be migrated on login. New hashes always use PBKDF2.

## Production configuration

The core reads these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | unset | Application secret reserved for integrations |
| `JWT_SECRET` | unset | HS256 signing key |
| `DATABASE_URL` | `sqlite:///app.db` | SQLite or PostgreSQL URL |
| `JWT_EXPIRE_MINUTES` | `30` | Token lifetime |
| `CORS_ORIGINS` | unset | Comma-separated allowed origins |
| `RATE_LIMIT_REQUESTS` | `100` | Requests per client/endpoint window |
| `RATE_LIMIT_WINDOW` | `3600` | Rate-limit window in seconds |
| `REDIS_URL` | unset | Optional Redis URL |
| `GRPC_PORT` | `50051` | Optional gRPC service port |
| `WEBSOCKET_PORT` | `8765` | Optional WebSocket service port |
| `PROMETHEUS_PORT` | `9090` | Optional standalone metrics port |

For `production=True`, `Config.validate_production()` requires non-empty
`SECRET_KEY`, `JWT_SECRET`, and an explicit `CORS_ORIGINS` list without `*`:

```bash
export SECRET_KEY='use-a-secret-manager'
export JWT_SECRET='use-a-different-secret'
export CORS_ORIGINS='https://app.example.com'
python -c 'from vedrakit import run; run(production=True, port=8080)'
```

Bind production servers to `0.0.0.0`; development defaults to `127.0.0.1`.
Do not commit these secrets or use the development fallback in a public
deployment.

## SQLite and PostgreSQL models

The same annotated `Model` API works with SQLite and PostgreSQL. Select the
backend using `DATABASE_URL`:

```bash
# Local development
export DATABASE_URL='sqlite:///app.db'

# Production PostgreSQL
export DATABASE_URL='postgresql://app_user:strong-password@db.example.com:5432/app'
```

PostgreSQL uses the optional psycopg 3 driver:

```bash
pip install -e ".[postgresql]"
```

Models use annotated fields and backend-appropriate types and identity
columns:

```python
from vedrakit import Database, Migration, Model


class Product(Model):
    id: int
    name: str
    price: float


Product.create_table()
product = Product(name="Notebook", price=12.5).save()
same_product = Product.get(product.id)
all_products = Product.all()
```

Migrations are idempotent and recorded in the `migrations` table:

```python
Migration.init_migration_table()
Migration.add_migration(
    "add-product-index",
    "CREATE INDEX product_name_idx ON products(name)",
)
Migration.revert_migration(
    "add-product-index",
    "DROP INDEX product_name_idx",
)
```

Call `Database.close_all()` during controlled shutdown or test teardown.
Connections are scoped per worker thread so PostgreSQL connections are not
shared across HTTP worker threads. PostgreSQL migrations support the same
idempotent `Migration.add_migration()` and `revert_migration()` API. Keep
migration scripts to discrete SQL statements separated by semicolons.

## Caching and rate limiting

`@cache` supports both synchronous and asynchronous functions. Redis is used
when `REDIS_URL` and the optional `redis` package are available. If Redis is
unavailable, the cache and rate limiter use a process-local, thread-safe
fallback so a development server remains usable:

```python
from vedrakit import cache


@cache(timeout=300)
def expensive_lookup(user_id: int):
    return {"user_id": user_id}
```

The in-memory fallback is not shared between processes. Use Redis for
multi-worker deployments.

## Background work

`TaskQueue` uses worker threads and supports both sync and async callables:

```python
import asyncio
from vedrakit import TaskQueue, background_task


queue = TaskQueue("emails")
queue.start_workers(2)
asyncio.run(queue.add_task(send_email, "user@example.com"))


@background_task
def rebuild_index():
    ...
```

Call `queue.stop()` during shutdown. The default `background_task` decorator
uses the started `default` queue and otherwise creates a daemon thread.
Background exceptions are logged and do not crash the HTTP worker.

## Static files and CORS

`App(static_dir="static")` serves files below `/static/`. The real path is
checked before opening a file, so `../` traversal cannot escape the static
root. Missing files return `404`.

CORS responses are only allowed for origins in `Config.CORS_ORIGINS`. In
production, use explicit origins rather than `*`, especially when cookies or
authorization headers are involved. HTTP responses also include
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and a strict
`Referrer-Policy` by default.

## OpenAPI documentation

`GET /docs` returns an OpenAPI 3.0.3 JSON document generated from registered
routes. It includes:

- Route methods and summaries from docstrings
- Path and query parameters
- JSON request bodies for `BaseModel` inputs
- Model schemas and required fields

The document is intentionally returned as JSON so it can be consumed by
Swagger UI, Redoc, code generators, or an API gateway.

## GraphQL, WebSocket, and gRPC

These integrations are optional so the core package remains dependency-light.

### GraphQL

Install `graphene`, create a schema, and register it:

```python
import graphene
from vedrakit import graphql_schema


class Query(graphene.ObjectType):
    hello = graphene.String()

    def resolve_hello(self, info):
        return "Hello from Vedrakit"


@graphql_schema(graphene.Schema(query=Query))
def api_schema():
    pass
```

POST a GraphQL JSON body to `/graphql`:

```bash
curl -X POST http://127.0.0.1:8080/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ hello }"}'
```

### WebSocket

Install `websockets`, register a handler, and run the optional service:

```python
from vedrakit import WebSocketManager, run_websocket_server, websocket_endpoint


@websocket_endpoint("/ws")
async def echo(websocket, path):
    WebSocketManager.add_connection(path, websocket)
    try:
        async for message in websocket:
            await websocket.send(f"Echo: {message}")
    finally:
        WebSocketManager.remove_connection(path, websocket)


run_websocket_server()
```

### gRPC

Install `grpcio` and pass generated bindings to the server:

```python
from vedrakit import run_grpc_server
from generated_pb2_grpc import add_MyServiceServicer_to_server


server = run_grpc_server(
    servicer=MyService(),
    add_servicer=add_MyServiceServicer_to_server,
)
server.wait_for_termination()
```

The prototype's previous gRPC placeholder has been replaced with a real
server factory; service definitions remain application-specific and should be
generated from your `.proto` files.

## Metrics and operations

The framework records request count, active requests, and request duration.
The built-in `/metrics` endpoint emits Prometheus-compatible text. A separate
daemon metrics server is also available:

```python
from vedrakit import run_metrics_server

run_metrics_server(port=9090)
```

Health/readiness semantics:

- `/health` checks process liveness and does not require the database
- `/ready` performs `SELECT 1` against the configured database

## Testing

Run the complete suite:

```bash
python -m unittest discover -s tests -v
python -m py_compile vedrakit/*.py
```

The tests cover HTTP behavior through real local sockets, not only direct
function calls. They cover validation, response models, query and path
coercion, async routes, auth and RBAC, status codes, CORS, static traversal,
OpenAPI, readiness, password/JWT security, SQLite CRUD, migrations, cache
fallback, and task workers.

## Package layout

```text
vedrakit/
  __init__.py       Public API
  core.py           Runtime implementation
examples/
  basic_app.py      Runnable example
tests/
  test_vedrakit.py End-to-end unit tests
pyproject.toml      Package metadata and optional extras
```

## License

MIT