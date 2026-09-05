import json
import os
import sys
import tempfile
import threading
import time
import unittest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from unittest.mock import patch

from vedrakit import (
    AdvancedMiniFlask,
    App,
    BaseModel,
    Config,
    Database,
    Migration,
    Model,
    Query,
    RequestHandler,
    Role,
    Security,
    TaskQueue,
    User,
    UserCreate,
    UserResponse,
    WebSocketManager,
    background_task,
    cache,
    depends,
    generate_latest,
    requires_role,
)


class CreatePayload(BaseModel):
    name: str
    count: int


class ItemResponse(BaseModel):
    id: int
    name: str


class Item(Model):
    id: int
    name: str
    count: int


class VedrakitTestCase(unittest.TestCase):
    def setUp(self):
        self.old_database_url = Config.DATABASE_URL
        self.old_origins = Config.CORS_ORIGINS
        self.old_rate_limit = Config.RATE_LIMIT_REQUESTS
        self.temp_dir = tempfile.TemporaryDirectory()
        Config.DATABASE_URL = os.environ.get(
            "VEDRAKIT_POSTGRES_URL",
            f"sqlite:///{os.path.join(self.temp_dir.name, 'test.db')}",
        )
        Config.CORS_ORIGINS = ["http://example.test"]
        Config.RATE_LIMIT_REQUESTS = 1000
        Database.close_all()
        Migration._migrations_applied.clear()
        Security._rate_limit_store.clear()

    def tearDown(self):
        Database.close_all()
        Config.DATABASE_URL = self.old_database_url
        Config.CORS_ORIGINS = self.old_origins
        Config.RATE_LIMIT_REQUESTS = self.old_rate_limit
        self.temp_dir.cleanup()

    def make_server(self, app):
        server = app.make_server("127.0.0.1", 0, production=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        return server

    def request(self, server, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        payload = None
        request_headers = headers or {}
        if body is not None:
            payload = json.dumps(body).encode()
            request_headers = {"Content-Type": "application/json", **request_headers}
        connection.request(method, path, payload, request_headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = raw.decode()
        connection.close()
        return response.status, data, response.getheaders()

    def test_security_hashes_and_jwt(self):
        with self.assertRaises(ValueError):
            Security.hash_password("short")
        hashed = Security.hash_password("correct horse battery staple")
        self.assertTrue(Security.verify_password("correct horse battery staple", hashed))
        self.assertFalse(Security.verify_password("wrong password", hashed))

        Config.JWT_SECRET = "test-jwt-secret"
        token = Security.create_jwt({"sub": "user-1", "role": "admin"})
        claims = Security.verify_jwt(token)
        self.assertEqual(claims["sub"], "user-1")
        self.assertEqual(claims["role"], "admin")

    def test_model_and_migrations(self):
        Item.create_table()
        created = Item(name="Widget", count=2).save()
        self.assertIsNotNone(created.id)
        row = Item.get(created.id)
        self.assertEqual(dict(row), {"id": created.id, "name": "Widget", "count": 2})
        created.count = 3
        created.save()
        self.assertEqual(Item.get(created.id)["count"], 3)

        Migration.add_migration("add-index", "CREATE INDEX item_name_idx ON items(name)")
        self.assertIn("add-index", Migration._migrations_applied)
        Migration.add_migration("add-index", "SELECT 1")
        Migration.revert_migration("add-index", "DROP INDEX item_name_idx")
        self.assertNotIn("add-index", Migration._migrations_applied)

        User.create_table()
        user = User.create("ada", "ada@example.com", "correct horse battery staple", Role.ADMIN)
        self.assertTrue(user.verify_password("correct horse battery staple"))
        self.assertFalse(user.verify_password("wrong password"))
        self.assertEqual(UserCreate.__name__, "UserCreate")
        self.assertEqual(UserResponse.__name__, "UserResponse")
        self.assertIs(AdvancedMiniFlask, RequestHandler)

    def test_database_backend_selection_and_postgres_driver_boundary(self):
        self.assertEqual(Database.backend("sqlite:///local.db"), "sqlite")
        self.assertEqual(
            Database.backend("postgresql://user:password@localhost/app"),
            "postgresql",
        )
        with self.assertRaises(ValueError):
            Database.backend("mysql://localhost/app")

        class FakePostgresConnection:
            __module__ = "psycopg"

            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))
                return self

        fake = FakePostgresConnection()
        self.assertTrue(Database.is_postgresql(fake))
        Database.execute(fake, "SELECT * FROM users WHERE id = ?", (7,))
        self.assertEqual(fake.calls[-1], ("SELECT * FROM users WHERE id = %s", (7,)))

        Database.close_all()
        with patch.dict(sys.modules, {"psycopg": None}):
            with self.assertRaisesRegex(RuntimeError, "requires psycopg"):
                Database.get_connection("postgresql://user:password@localhost/app")

    def test_routes_query_body_async_auth_and_error_handling(self):
        app = App(static_dir=self.temp_dir.name)

        @app.route("/items/{item_id}", ["GET"], response_model=ItemResponse)
        def item(item_id: int):
            return {"id": item_id, "name": "Item"}

        @app.route("/items", ["POST"], response_model=ItemResponse)
        def create(payload: CreatePayload):
            return {"id": payload.count, "name": payload.name}

        @app.route("/page", ["GET"])
        def page(limit: int = Query(default=10)):
            return {"limit": limit}

        @app.route("/async", ["GET"])
        async def async_route():
            return {"ok": True}

        def current_user(req):
            return req.request_context["user"]

        @app.route("/secure", ["GET"], require_auth=True, required_roles=[Role.ADMIN])
        @depends(current_user)
        def secure(user: dict):
            return {"user": user["sub"]}

        server = self.make_server(app)
        Config.JWT_SECRET = "test-jwt-secret"
        status, data, _ = self.request(server, "GET", "/items/7")
        self.assertEqual((status, data), (200, {"id": 7, "name": "Item"}))
        status, data, _ = self.request(server, "GET", "/items/not-an-int")
        self.assertEqual(status, 400)
        self.assertIn("Invalid", data["error"])
        status, data, _ = self.request(server, "POST", "/items", {"name": "Box", "count": 4})
        self.assertEqual((status, data), (200, {"id": 4, "name": "Box"}))
        status, data, _ = self.request(server, "POST", "/items", {"name": "Box"})
        self.assertEqual(status, 400)
        status, data, _ = self.request(server, "GET", "/page?limit=25")
        self.assertEqual(data, {"limit": 25})
        status, data, _ = self.request(server, "GET", "/async")
        self.assertEqual((status, data), (200, {"ok": True}))
        status, data, _ = self.request(server, "GET", "/secure")
        self.assertEqual(status, 401)
        token = Security.create_jwt({"sub": "admin-1", "role": "admin"})
        status, data, _ = self.request(server, "GET", "/secure", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual((status, data), (200, {"user": "admin-1"}))

    def test_docs_health_readiness_cors_and_static_traversal(self):
        static_root = os.path.join(self.temp_dir.name, "static")
        os.makedirs(static_root)
        with open(os.path.join(static_root, "hello.txt"), "w") as file:
            file.write("hello")
        with open(os.path.join(self.temp_dir.name, "secret.txt"), "w") as file:
            file.write("not public")
        app = App(static_dir=static_root)

        @app.route("/hello", ["GET"])
        def hello():
            return {"hello": "world"}

        server = self.make_server(app)
        status, data, headers = self.request(server, "GET", "/health", headers={"Origin": "http://example.test"})
        self.assertEqual((status, data), (200, {"status": "healthy"}))
        self.assertIn(("Access-Control-Allow-Origin", "http://example.test"), headers)
        status, data, _ = self.request(server, "GET", "/ready")
        self.assertEqual((status, data), (200, {"status": "ready"}))
        status, data, _ = self.request(server, "GET", "/docs")
        self.assertEqual(status, 200)
        self.assertIn("/hello", data["paths"])
        status, data, _ = self.request(server, "GET", "/static/hello.txt")
        self.assertEqual((status, data), (200, "hello"))
        status, _, _ = self.request(server, "GET", "/static/../secret.txt")
        self.assertEqual(status, 404)
        status, _, headers = self.request(server, "POST", "/hello")
        self.assertEqual(status, 405)
        self.assertTrue(any(name == "Allow" for name, _ in headers))

    def test_cache_and_task_queue(self):
        calls = []

        @cache(timeout=5)
        def cached(value):
            calls.append(value)
            return {"value": value}

        self.assertEqual(cached("x"), {"value": "x"})
        self.assertEqual(cached("x"), {"value": "x"})
        self.assertEqual(calls, ["x"])

        completed = threading.Event()
        queue = TaskQueue("test")

        def work(value):
            if value == 42:
                completed.set()

        queue.start_workers(1)
        queue.tasks.put((work, (42,), {}))
        self.assertTrue(completed.wait(2))
        queue.stop()

        async_calls = []

        @cache(timeout=5)
        async def async_cached(value):
            async_calls.append(value)
            return {"value": value}

        async def exercise_async_cache():
            self.assertEqual(await async_cached("y"), {"value": "y"})
            self.assertEqual(await async_cached("y"), {"value": "y"})

        asyncio.run(exercise_async_cache())
        self.assertEqual(async_calls, ["y"])

        background_done = threading.Event()

        @background_task
        def background_work():
            background_done.set()

        background_work()
        self.assertTrue(background_done.wait(2))

    def test_middleware_exceptions_role_decorator_graphql_and_metrics(self):
        app = App()

        @app.middleware
        def header_middleware(request):
            return request.headers.get("X-Allow") == "yes"

        @app.exception_handler(ValueError)
        def handle_bad_request(error):
            return 422, {"custom": str(error)}

        @app.route("/role", ["GET"], require_auth=True)
        @requires_role(Role.MODERATOR)
        def role_route():
            return {"ok": True}

        @app.route("/bad", ["GET"])
        def bad_route():
            raise ValueError("bad input")

        class FakeResult:
            data = {"hello": "world"}
            errors = []

        class FakeSchema:
            def execute(self, query, variables=None, operation_name=None):
                self.last_query = query
                return FakeResult()

        app.graphql_schemas["test"] = FakeSchema()
        server = self.make_server(app)
        status, data, _ = self.request(server, "GET", "/bad", headers={"X-Allow": "yes"})
        self.assertEqual((status, data), (422, {"custom": "bad input"}))
        status, data, _ = self.request(server, "GET", "/bad")
        self.assertEqual(status, 403)

        Config.JWT_SECRET = "test-jwt-secret"
        token = Security.create_jwt({"sub": "user-1", "role": "user"})
        status, _, _ = self.request(
            server, "GET", "/role", headers={"X-Allow": "yes", "Authorization": f"Bearer {token}"}
        )
        self.assertEqual(status, 403)
        token = Security.create_jwt({"sub": "mod-1", "role": "moderator"})
        status, data, _ = self.request(
            server, "GET", "/role", headers={"X-Allow": "yes", "Authorization": f"Bearer {token}"}
        )
        self.assertEqual((status, data), (200, {"ok": True}))
        status, data, _ = self.request(
            server,
            "POST",
            "/graphql",
            {"query": "{ hello }"},
            headers={"X-Allow": "yes"},
        )
        self.assertEqual((status, data), (200, {"data": {"hello": "world"}}))
        self.assertIn(b"http_requests_total", generate_latest())

    def test_websocket_manager_and_production_config(self):
        class FakeSocket:
            def __init__(self):
                self.messages = []

            async def send(self, message):
                self.messages.append(message)

        socket = FakeSocket()
        WebSocketManager.add_connection("/ws", socket)
        asyncio.run(WebSocketManager.broadcast("/ws", "hello"))
        self.assertEqual(socket.messages, ["hello"])
        WebSocketManager.remove_connection("/ws", socket)

        old = (Config.SECRET_KEY, Config.JWT_SECRET, Config.CORS_ORIGINS)
        Config.SECRET_KEY = ""
        Config.JWT_SECRET = ""
        Config.CORS_ORIGINS = ["*"]
        with self.assertRaises(RuntimeError):
            Config.validate_production()
        Config.SECRET_KEY, Config.JWT_SECRET, Config.CORS_ORIGINS = old


@unittest.skipUnless(
    os.environ.get("VEDRAKIT_POSTGRES_URL"),
    "set VEDRAKIT_POSTGRES_URL to run live PostgreSQL integration tests",
)
class PostgreSQLIntegrationTest(unittest.TestCase):
    """Exercise the database APIs against a real PostgreSQL server.

    The test is opt-in so the standard-library-focused test suite remains
    runnable without the optional psycopg dependency or a database service.
    The CI PostgreSQL job supplies a disposable service and this URL.
    """

    database_url = os.environ.get("VEDRAKIT_POSTGRES_URL", "")
    migration_name = "postgres-live-integration-migration"
    migration_table = "postgres_live_migration_probe"

    def setUp(self):
        self.old_database_url = Config.DATABASE_URL
        Config.DATABASE_URL = self.database_url
        Database.close_all()
        Migration._migrations_applied.clear()

        connection = Database.get_connection()
        with Database.transaction(connection):
            Database.execute(
                connection,
                "DROP TABLE IF EXISTS postgresintegrationitems",
            )
            Database.execute(
                connection,
                f"DROP TABLE IF EXISTS {self.migration_table}",
            )
            Database.execute(
                connection,
                "DROP TABLE IF EXISTS migrations",
            )
        Migration.init_migration_table()

    def tearDown(self):
        Database.close_all()
        Config.DATABASE_URL = self.old_database_url
        Migration._migrations_applied.clear()

    def test_live_crud_identity_migrations_transactions_and_readiness(self):
        class PostgresIntegrationItem(Model):
            id: int
            name: str
            count: int

        PostgresIntegrationItem.create_table()
        first = PostgresIntegrationItem(name="first", count=1).save()
        second = PostgresIntegrationItem(name="second", count=2).save()

        self.assertIsInstance(first.id, int)
        self.assertIsInstance(second.id, int)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            dict(PostgresIntegrationItem.get(first.id)),
            {"id": first.id, "name": "first", "count": 1},
        )

        first.count = 3
        first.save()
        self.assertEqual(PostgresIntegrationItem.get(first.id)["count"], 3)
        self.assertEqual(len(PostgresIntegrationItem.all()), 2)

        Migration.add_migration(
            self.migration_name,
            f"CREATE TABLE {self.migration_table} "
            "(id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, value TEXT)",
            f"DROP TABLE {self.migration_table}",
        )
        self.assertIn(self.migration_name, Migration._migrations_applied)
        self.assertEqual(
            Database.execute(
                Database.get_connection(),
                "SELECT name FROM migrations WHERE name = ?",
                (self.migration_name,),
            ).fetchone()["name"],
            self.migration_name,
        )
        # Reapplying an already recorded migration must not execute its DDL.
        Migration.add_migration(
            self.migration_name,
            f"CREATE TABLE {self.migration_table} (value TEXT)",
        )
        Migration.revert_migration(
            self.migration_name,
            f"DROP TABLE {self.migration_table}",
        )
        self.assertNotIn(self.migration_name, Migration._migrations_applied)
        self.assertIsNone(
            Database.execute(
                Database.get_connection(),
                "SELECT to_regclass(?) AS table_name",
                (self.migration_table,),
            ).fetchone()["table_name"],
        )

        connection = Database.get_connection()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            try:
                with Database.transaction(connection):
                    Database.execute(
                        connection,
                        "INSERT INTO postgresintegrationitems (name, count) "
                        "VALUES (?, ?)",
                        ("rolled-back", 99),
                    )
                    raise RuntimeError("rollback")
            except RuntimeError:
                raise
        self.assertIsNone(
            Database.execute(
                connection,
                "SELECT id FROM postgresintegrationitems WHERE name = ?",
                ("rolled-back",),
            ).fetchone(),
        )

        app = App()
        self.assertTrue(app.check_readiness())
        connection.close()
        self.assertFalse(app.check_readiness())

    def test_concurrent_workers_receive_independent_postgres_connections(self):
        class PostgresWorkerItem(Model):
            id: int
            name: str
            count: int

        PostgresWorkerItem.create_table()

        def save_item(index):
            connection = Database.get_connection()
            item = PostgresWorkerItem(name=f"worker-{index}", count=index).save()
            return threading.get_ident(), id(connection), item.id

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(save_item, range(12)))

        connection_ids_by_thread = {}
        for thread_id, connection_id, item_id in results:
            connection_ids_by_thread.setdefault(thread_id, set()).add(connection_id)
            self.assertIsInstance(item_id, int)
        self.assertGreaterEqual(len(connection_ids_by_thread), 2)
        self.assertTrue(all(len(ids) == 1 for ids in connection_ids_by_thread.values()))
        self.assertEqual(
            Database.execute(
                Database.get_connection(),
                "SELECT COUNT(*) AS count FROM postgresworkeritems",
            ).fetchone()["count"],
            12,
        )


if __name__ == "__main__":
    unittest.main()