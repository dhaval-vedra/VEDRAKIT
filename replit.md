# Vedrakit Production Framework

Vedrakit is a dependency-light Python web framework with routing, validation,
authentication, persistence, background work, documentation, and observability.

## Run & Operate

- `python -m examples.basic_app` — run the Vedrakit example on port 8080
- `python -m unittest discover -s tests -v` — run the full Python test suite
- `python -m py_compile vedrakit/*.py` — syntax check the package
- `vedrakit new <name>` — scaffold a runnable project
- `vedrakit openapi app:app --output openapi.json` — export OpenAPI
- `vedrakit client app:app --output api-client.ts` — generate a typed client
- `pip install -e ".[all]"` — install optional Redis, GraphQL, WebSocket, and gRPC extras

The preconfigured TypeScript API-server and mockup workflows belong to the
workspace scaffold and are not the Vedrakit runtime.

## Stack

- Python 3.10+
- Core HTTP server: Python standard library
- Persistence: SQLite and PostgreSQL with a small annotated model layer
- Validation: Vedrakit `BaseModel`
- Optional services: Redis, Graphene, websockets, grpcio

## Where things live

- `vedrakit/core.py` — source of truth for the runtime and public compatibility API
- `vedrakit/__init__.py` — public exports
- `vedrakit/cli.py` — dependency-free CLI and project scaffolding
- `vedrakit/codegen.py` — typed TypeScript client generator
- `examples/basic_app.py` — runnable API example
- `examples/complete_app.py` — complete CRUD API example
- `tests/test_vedrakit.py` — socket-level and unit coverage
- `tests/test_tooling.py` — CLI, OpenAPI, and client-generation coverage
- `README.md` — installation and complete API documentation
- `pyproject.toml` — package metadata and optional extras

## Architecture decisions

- The core has no mandatory third-party runtime dependency; integrations are optional.
- `App` instances isolate routes and state for testability and multi-service use.
- OpenAPI metadata is declared at the application and route level, then reused
  by the CLI and typed client generator.
- Redis failures fall back to thread-safe in-memory cache/rate limiting for local resilience; multi-process deployments should configure Redis.
- New passwords use PBKDF2-HMAC-SHA256 while legacy prototype hashes remain verifiable for migration.
- Production mode rejects missing secrets and wildcard CORS rather than silently using insecure defaults.

## Product

Vedrakit provides a compact Python API framework with FastAPI-style decorators,
typed request/response models, SQLite models and migrations, JWT/RBAC auth,
middleware and dependency injection, background queues, OpenAPI docs,
health/readiness checks, metrics, and optional GraphQL/WebSocket/gRPC services.

## User preferences

The requested feature surface must remain complete relative to the uploaded
prototype, with production fixes, tests, and documentation delivered together.

## Gotchas

- `DATABASE_URL` supports `sqlite:///...` and `postgresql://...` URLs.
- PostgreSQL uses the optional `psycopg` extra and `postgresql://...` URLs.
- Use explicit `CORS_ORIGINS` in production; `*` is intentionally rejected.
- Start a `TaskQueue` worker before expecting `background_task` to use it.
- WebSocket, GraphQL, Redis, and gRPC require their corresponding optional extra.

## Pointers

- See `README.md` for the complete public API and operational guide.
