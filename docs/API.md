# Vedrakit API reference

This is the complete public API reference for the current Vedrakit runtime.
The package is intentionally small: the core uses only the Python standard
library, while PostgreSQL, Redis, GraphQL, WebSocket, and gRPC support are
optional extras.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# The same install provides the CLI
vedrakit --help

# Optional adapters
python -m pip install -e ".[postgresql]"
python -m pip install -e ".[redis]"
python -m pip install -e ".[graphql]"
python -m pip install -e ".[websocket]"
python -m pip install -e ".[grpc]"
```

The installation also provides a dependency-free CLI:

```bash
vedrakit new inventory-api
vedrakit dev app:app
vedrakit routes app:app
vedrakit openapi app:app --output openapi.json
vedrakit client app:app --output api-client.ts
```

Targets use `module:attribute` syntax. `vedrakit new` creates an application,
tests, package metadata, environment template, and README.

## Public exports

The public symbols are re-exported from `vedrakit`:

| Area | Symbols |
| --- | --- |
| Application | `App`, `app`, `run`, `RequestHandler`, `AdvancedMiniFlask` |
| Routing | `route`, `get`, `post`, `put`, `delete` |
| Validation | `BaseModel`, `Query`, `Path`, `Param`, `ParamTypes` |
| Security | `Security`, `Role`, `User`, `UserCreate`, `UserResponse`, `get_current_user`, `requires_role` |
| Persistence | `Database`, `Migration`, `Model` |
| Runtime | `TaskQueue`, `background_task`, `cache`, `RedisManager` |
| Operations | `generate_latest`, `run_metrics_server`, `configure_logging` |
| Code generation | `generate_typescript_client`, `generate_typescript_client_file` |
| Optional services | `websocket_endpoint`, `WebSocketManager`, `run_websocket_server`, `graphql_schema`, `run_grpc_server` |

## Application and routing

### `App(static_dir="static", title="Vedrakit API", version="1.1.0", description="", servers=None)`

Creates an isolated application object. Use an `App` instance for production
services and tests. Every instance owns its routes, middleware, exception
handlers, static directory, and OpenAPI document. `title`, `version`,
`description`, and `servers` populate the OpenAPI `info` and `servers` fields.

```python
from vedrakit import App

app = App(static_dir="public")
```

### `App.route(path, methods, response_model=None, require_auth=False, required_roles=None, summary=None, description=None, tags=None, operation_id=None, deprecated=False, response_description="Successful response")`

Registers a route. `methods` accepts `GET`, `POST`, `PUT`, `PATCH`, and
`DELETE`. Path parameters use `{name}` syntax. Metadata arguments enrich the
generated OpenAPI operation without changing request handling. `App.get`,
`App.post`, `App.put`, `App.patch`, and `App.delete` are convenience wrappers.

```python
from vedrakit import App, Role

app = App()

@app.route(
    "/projects/{project_id}",
    ["GET"],
    require_auth=True,
    required_roles=[Role.USER, Role.ADMIN],
)
def get_project(project_id: int):
    """Return one project."""
    return {"id": project_id, "name": "Vedrakit"}
```

Decorators return the original function, so normal Python testing and
composition remain possible.

### Shortcut decorators

The global decorators register routes on the module-level `vedrakit.app`:

```python
from vedrakit import delete, get, post, put, run

@get("/health-check")
def health_check():
    return {"ok": True}

@post("/items")
def create_item():
    return 201, {"created": True}

@put("/items/{item_id}")
def update_item(item_id: int):
    return {"updated": item_id}

@delete("/items/{item_id}")
def delete_item(item_id: int):
    return 204, ""

run()
```

The equivalent generic decorator is:

```python
@app.route("/items/{item_id}", ["GET", "PUT"])
def item(item_id: int):
    ...
```

### Handler return values

Handlers may return:

```python
{"json": "object"}                  # 200 application/json
"plain text"                        # 200 text/plain
(201, {"created": True})             # explicit status + JSON
(204, "")                            # explicit status + text
```

Mapping values and dataclasses are serialized as JSON. A `response_model`
validates successful mapping responses before they are sent.

## Request validation

### `BaseModel`

Declare annotated request or response fields. Fields without class defaults
are required. Supported coercions include `str`, `int`, `float`, `bool`,
`bytes`, optional unions, nested `BaseModel` values, and `Enum` values.

```python
from vedrakit import BaseModel

class CreateItem(BaseModel):
    name: str
    quantity: int
    featured: bool = False

payload = CreateItem.parse_obj({
    "name": "Notebook",
    "quantity": "3",
    "featured": "yes",
})

assert payload.dict() == {
    "name": "Notebook",
    "quantity": 3,
    "featured": True,
}
```

`BaseModel.validate(data)` returns a validated dictionary. Invalid or missing
fields raise `ValueError`; the HTTP adapter turns that into a `400` response.

### Query and path parameters

Use `Query` to document an optional query value and provide a default:

```python
from vedrakit import Query

@app.route("/items", ["GET"])
def list_items(
    skip: int = Query(default=0, description="Number of records to skip"),
    limit: int = Query(default=20, description="Maximum records to return"),
):
    return {"skip": skip, "limit": limit}
```

Path values are inferred from the route template:

```python
@app.route("/items/{item_id}", ["GET"])
def read_item(item_id: int):
    return {"id": item_id}
```

The raw `Path(description="...")` marker is available for API compatibility;
the route template remains the source of truth for path extraction.

### Request body formats

For `POST`, `PUT`, and `PATCH`:

- `application/json` must contain a JSON object.
- `application/x-www-form-urlencoded` is converted to a dictionary.
- Other content types are exposed as `{"raw_body": bytes}`.
- Bodies larger than 10 MiB are rejected.

## Middleware, dependencies, and errors

### Middleware

Middleware receives the `RequestHandler`. Return `False` to stop the request
with `403`; return `True` to continue.

```python
@app.middleware
def require_internal_header(request):
    return request.headers.get("X-Internal") == "yes"
```

### Dependencies

`depends(function)` attaches a dependency to a route. Dependencies may be
synchronous or asynchronous and can receive already-parsed values or the
request object using `req`, `request`, or `handler`.

```python
from vedrakit import depends

def request_id(req):
    return req.headers.get("X-Request-ID", "generated-locally")

@app.route("/request-info", ["GET"])
@depends(request_id)
def request_info(request_id):
    return {"request_id": request_id}
```

### Exception handlers

Register a handler for an exception class. The handler can return any normal
Vedrakit response.

```python
@app.exception_handler(ValueError)
def validation_error(error):
    return 422, {"error": str(error), "type": "validation_error"}
```

## Authentication and authorization

### Passwords

`Security.hash_password()` creates PBKDF2-HMAC-SHA256 hashes with a random
salt and 310,000 iterations. Passwords must be at least eight characters.
`verify_password()` also understands the original prototype's salted SHA-256
format so existing accounts can migrate during login.

```python
from vedrakit import Security

stored_hash = Security.hash_password("correct horse battery staple")
if Security.verify_password(password_from_form, stored_hash):
    print("authenticated")
```

### JWT

Set `Config.JWT_SECRET` before creating or verifying tokens. Tokens use HS256
and include `iat` and `exp`.

```python
from vedrakit import Config, Security

Config.JWT_SECRET = "load-this-from-a-secret-manager"
token = Security.create_jwt({"sub": "user-42", "role": "admin"})
claims = Security.verify_jwt(token)
```

Send the token with:

```http
Authorization: Bearer <token>
```

### Route protection

```python
from vedrakit import Role

@app.route(
    "/admin/report",
    ["GET"],
    require_auth=True,
    required_roles=[Role.ADMIN],
)
def admin_report():
    return {"status": "private"}
```

`requires_role(Role.ADMIN)` can also be used as a decorator. The authenticated
claims are available through `get_current_user`:

```python
from vedrakit import depends, get_current_user

@app.route("/me", ["GET"], require_auth=True)
@depends(get_current_user)
def me(user):
    return {"subject": user["sub"], "role": user.get("role")}
```

## Persistence

### Database URLs

```bash
DATABASE_URL=sqlite:///app.db
DATABASE_URL=postgresql://user:password@host:5432/app
```

PostgreSQL requires `psycopg`:

```bash
pip install -e ".[postgresql]"
```

Connections are cached per database URL and worker thread. Call
`Database.close_all()` during controlled shutdown.

### `Model`

Annotated fields become table columns. The default table name is the lowercase
class name plus `s`.

```python
from vedrakit import Model

class Product(Model):
    id: int
    name: str
    price: float

Product.create_table()
created = Product(name="Notebook", price=12.5).save()
one = Product.get(created.id)
many = Product.all()

created.price = 15.0
created.save()
```

Supported storage types:

| Python annotation | SQLite | PostgreSQL |
| --- | --- | --- |
| `int`, `bool` | `INTEGER` | `INTEGER` / `BOOLEAN` |
| `float` | `REAL` | `DOUBLE PRECISION` |
| `bytes` | `BLOB` | `BYTEA` |
| other supported values | `TEXT` | `TEXT` |

### `Migration`

Migrations are recorded in the `migrations` table and are idempotent:

```python
from vedrakit import Migration

Migration.init_migration_table()
Migration.add_migration(
    "add-product-index",
    "CREATE INDEX product_name_idx ON products(name)",
)

# Run only when rolling back deliberately.
Migration.revert_migration(
    "add-product-index",
    "DROP INDEX product_name_idx",
)
```

Keep migration SQL to discrete semicolon-separated statements. Vedrakit
converts its portable `?` placeholders to `%s` for PostgreSQL.

## Cache, rate limiting, and background tasks

### `cache(timeout=60)`

Works with sync and async functions. Redis is used when configured and
available; otherwise a process-local cache is used.

```python
from vedrakit import cache

@cache(timeout=300)
def get_catalog(category: str):
    return {"category": category, "items": []}
```

The memory fallback is not shared between processes. Use Redis for a
multi-worker deployment.

### `TaskQueue`

```python
import asyncio
from vedrakit import TaskQueue

queue = TaskQueue("emails")
queue.start_workers(2)

async def enqueue():
    await queue.add_task(send_email, "user@example.com")

asyncio.run(enqueue())
queue.stop()
```

Both normal and async callables are accepted. Worker exceptions are logged.
`background_task` is a convenient decorator that uses the default queue when
workers are running, otherwise a daemon thread:

```python
from vedrakit import background_task

@background_task
def rebuild_search_index():
    ...

rebuild_search_index()
```

### Rate limiting

Every HTTP endpoint is checked by `Security.check_rate_limit(client_ip,
endpoint)`. Redis provides a shared counter when configured; the fallback is
thread-safe but process-local.

## Built-in endpoints and operations

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Process liveness; does not require the database |
| `GET /ready` | Runs `SELECT 1`; returns `503` when the database is unavailable |
| `GET /docs` | OpenAPI 3.0.3 JSON generated from registered routes |
| `GET /metrics` | Prometheus-compatible text exposition |
| `POST /graphql` | GraphQL execution when a schema is registered |
| `/static/<path>` | Static file serving with traversal protection |

```python
from vedrakit import generate_latest, run_metrics_server

metrics_payload = generate_latest()
metrics_server = run_metrics_server(port=9090)
```

Metrics include request count, active requests, and request duration.

### OpenAPI metadata

`App.openapi()` returns an OpenAPI 3.0.3 document. Application metadata is
configured when the app is created:

```python
app = App(
    title="Inventory API",
    version="2.0.0",
    description="Manage inventory items.",
    servers=[{"url": "https://api.example.com"}],
)
```

Route metadata is available on `App.route` and the `App.get`, `App.post`,
`App.put`, `App.patch`, and `App.delete` shortcuts:

```python
@app.get(
    "/items/{item_id}",
    summary="Read an item",
    tags=["items"],
    operation_id="getItem",
    deprecated=False,
    response_description="The requested item",
)
def get_item(item_id: int):
    """Return one item by ID."""
    return {"id": item_id}
```

The generator documents nested models, lists, unions, enums, dictionaries,
defaults, query descriptions, response schemas, JWT bearer security, and
required roles. Query markers support `description`, `example`, and
`required`:

```python
limit: int = Query(
    default=20,
    description="Maximum number of records",
    example=10,
)
```

### TypeScript client generation

Generate a fetch-based typed client from an app or from any OpenAPI mapping:

```bash
vedrakit client examples.complete_app:app --output todos-client.ts
```

Or use the Python API:

```python
from vedrakit import generate_typescript_client

typescript_source = generate_typescript_client(app.openapi())
```

The output includes TypeScript interfaces for component schemas, typed
operation methods, path/query serialization, JSON request bodies, and an
`ApiError` class. It has no runtime dependency beyond the platform `fetch`
implementation.

## Optional integrations

### GraphQL

```bash
pip install -e ".[graphql]"
```

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

### WebSocket

```bash
pip install -e ".[websocket]"
```

```python
from vedrakit import WebSocketManager, run_websocket_server, websocket_endpoint

@websocket_endpoint("/ws")
async def echo(websocket, path):
    WebSocketManager.add_connection(path, websocket)
    try:
        async for message in websocket:
            await websocket.send(message)
    finally:
        WebSocketManager.remove_connection(path, websocket)

run_websocket_server()
```

### gRPC

```bash
pip install -e ".[grpc]"
```

```python
from vedrakit import run_grpc_server
from generated_pb2_grpc import add_MyServiceServicer_to_server

server = run_grpc_server(
    servicer=MyService(),
    add_servicer=add_MyServiceServicer_to_server,
)
server.wait_for_termination()
```

The `.proto` file and generated bindings remain application-specific.

## Configuration reference

| Environment variable | Default | Notes |
| --- | --- | --- |
| `SECRET_KEY` | empty | Required by `run(production=True)` |
| `JWT_SECRET` | empty | Required for JWT issuance and production |
| `DATABASE_URL` | `sqlite:///app.db` | SQLite or PostgreSQL |
| `JWT_EXPIRE_MINUTES` | `30` | Token lifetime |
| `CORS_ORIGINS` | empty | Comma-separated explicit origins |
| `RATE_LIMIT_REQUESTS` | `100` | Requests per IP/endpoint window |
| `RATE_LIMIT_WINDOW` | `3600` | Window duration in seconds |
| `REDIS_URL` | empty | Optional shared cache/rate limiter |
| `GRPC_PORT` | `50051` | Optional gRPC listener |
| `WEBSOCKET_PORT` | `8765` | Optional WebSocket listener |
| `PROMETHEUS_PORT` | `9090` | Standalone metrics listener |

Production startup rejects missing `SECRET_KEY`, `JWT_SECRET`, and wildcard
CORS. Use a secret manager; never commit secrets.

## Testing and extension guidance

```bash
python -m unittest discover -s tests -v
python -m py_compile vedrakit/*.py examples/*.py tests/test_vedrakit.py

# Live PostgreSQL integration tests
export VEDRAKIT_POSTGRES_URL='postgresql://user:password@localhost:5432/vedrakit_test'
python -m unittest discover -s tests -v
```

Prefer `App` instances in tests to avoid global route state. Keep adapters
optional and raise a clear installation error when their package is absent.