from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.server
import inspect
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from concurrent import futures
from contextlib import contextmanager
from dataclasses import dataclass, is_dataclass, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path as FilePath
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Mapping,
    Optional,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

try:
    import redis as _redis
except ImportError:  # Redis is an optional production extra.
    _redis = None

try:
    import graphene as _graphene
except ImportError:
    _graphene = None

try:
    import websockets as _websockets
except ImportError:
    _websockets = None

try:
    import grpc as _grpc
except ImportError:
    _grpc = None


logger = logging.getLogger("vedrakit")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())


def configure_logging(log_file: Optional[str] = None, level: int = logging.INFO) -> None:
    """Configure Vedrakit logging without adding duplicate handlers."""
    logger.setLevel(level)
    if log_file and not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        logger.addHandler(RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5))


class Config:
    """Environment-backed settings. Call ``validate_production`` at startup."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    JWT_SECRET = os.environ.get("JWT_SECRET", "")
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "30"))
    CORS_ORIGINS = [x.strip() for x in os.environ.get("CORS_ORIGINS", "").split(",") if x.strip()]
    RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))
    RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "3600"))
    REDIS_URL = os.environ.get("REDIS_URL", "")
    GRPC_PORT = int(os.environ.get("GRPC_PORT", "50051"))
    WEBSOCKET_PORT = int(os.environ.get("WEBSOCKET_PORT", "8765"))
    PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "9090"))

    @classmethod
    def validate_production(cls) -> None:
        missing = []
        if not cls.SECRET_KEY:
            missing.append("SECRET_KEY")
        if not cls.JWT_SECRET:
            missing.append("JWT_SECRET")
        if not cls.CORS_ORIGINS or "*" in cls.CORS_ORIGINS:
            missing.append("CORS_ORIGINS")
        if missing:
            raise RuntimeError("Production configuration is incomplete: " + ", ".join(missing))


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "dict") and callable(value.dict):
        return value.dict()
    if isinstance(value, sqlite3.Row):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, separators=(",", ":"))


class Database:
    """Thread-aware connection registry for SQLite and PostgreSQL.

    PostgreSQL connections are intentionally optional. The core remains
    importable without psycopg, while a configured PostgreSQL URL fails with a
    useful installation message instead of a low-level import traceback.
    """

    _connections: Dict[tuple[str, int], Any] = {}
    _lock = threading.RLock()

    @classmethod
    def backend(cls, db_url: Optional[str] = None) -> str:
        db_url = db_url or Config.DATABASE_URL
        if db_url.startswith("sqlite:///"):
            return "sqlite"
        if db_url.startswith(("postgresql://", "postgres://")):
            return "postgresql"
        raise ValueError(
            "Unsupported DATABASE_URL. Use sqlite:///path.db or "
            "postgresql://user:password@host:5432/database."
        )

    @classmethod
    def get_connection(cls, db_url: Optional[str] = None) -> Any:
        db_url = db_url or Config.DATABASE_URL
        backend = cls.backend(db_url)
        key = (db_url, threading.get_ident())
        with cls._lock:
            if key not in cls._connections:
                if backend == "sqlite":
                    db_path = db_url[len("sqlite:///") :]
                    if not db_path:
                        raise ValueError("SQLite database path cannot be empty")
                    connection = sqlite3.connect(
                        db_path,
                        check_same_thread=False,
                        timeout=30,
                    )
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute("PRAGMA busy_timeout = 30000")
                else:
                    try:
                        import psycopg
                        from psycopg.rows import dict_row
                    except ImportError as exc:
                        raise RuntimeError(
                            "PostgreSQL support requires psycopg. "
                            "Install it with: pip install 'vedrakit[postgresql]'"
                        ) from exc
                    connection = psycopg.connect(
                        db_url,
                        connect_timeout=10,
                        row_factory=dict_row,
                    )
                cls._connections[key] = connection
            return cls._connections[key]

    @classmethod
    def is_postgresql(cls, connection: Any) -> bool:
        return connection.__class__.__module__.split(".", 1)[0] == "psycopg"

    @classmethod
    def execute(cls, connection: Any, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if cls.is_postgresql(connection):
            sql = sql.replace("?", "%s")
        return connection.execute(sql, params)

    @classmethod
    def execute_script(cls, connection: Any, sql: str) -> None:
        if not cls.is_postgresql(connection):
            connection.executescript(sql)
            return
        # Migration scripts are intentionally simple DDL statements. Splitting
        # here keeps the API dependency-light without requiring a SQL parser.
        statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
        for statement in statements:
            cls.execute(connection, statement)

    @classmethod
    @contextmanager
    def transaction(cls, connection: Any):
        """Commit a transaction without closing a cached connection.

        ``sqlite3.Connection`` uses its context manager for commit/rollback.
        psycopg's connection context manager also closes the connection on
        exit, which is incompatible with the per-thread connection registry.
        Explicit commit/rollback also handles an implicit transaction left by
        a preceding SELECT instead of nesting a savepoint that never commits.
        """
        if cls.is_postgresql(connection):
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
        else:
            with connection:
                yield connection

    @classmethod
    def close_all(cls) -> None:
        with cls._lock:
            for connection in cls._connections.values():
                try:
                    connection.close()
                except Exception:
                    logger.warning("Failed to close database connection", exc_info=True)
            cls._connections.clear()


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    """Read both psycopg dict rows and tuple-like rows."""
    if isinstance(row, Mapping):
        return row[key]
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index]


def _database_type(connection: Any, type_hint: Any) -> str:
    origin = get_origin(type_hint)
    if origin is Union:
        type_hint = next((arg for arg in get_args(type_hint) if arg is not type(None)), str)
    if Database.is_postgresql(connection):
        if type_hint is int:
            return "INTEGER"
        if type_hint is bool:
            return "BOOLEAN"
        if type_hint is float:
            return "DOUBLE PRECISION"
        if type_hint is bytes:
            return "BYTEA"
        return "TEXT"
    return _sqlite_type(type_hint)


class Migration:
    _migrations_applied: set[str] = set()

    @classmethod
    def init_migration_table(cls) -> None:
        connection = Database.get_connection()
        id_definition = (
            "id INTEGER PRIMARY KEY AUTOINCREMENT"
            if not Database.is_postgresql(connection)
            else "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        )
        with Database.transaction(connection):
            Database.execute(
                connection,
                f"""CREATE TABLE IF NOT EXISTS migrations (
                    {id_definition},
                    name TEXT UNIQUE NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
            )
        cls._migrations_applied = {
            _row_value(row, "name")
            for row in Database.execute(connection, "SELECT name FROM migrations").fetchall()
        }

    @classmethod
    def add_migration(cls, name: str, up_sql: str, down_sql: Optional[str] = None) -> None:
        cls.init_migration_table()
        if name in cls._migrations_applied:
            return
        connection = Database.get_connection()
        try:
            with Database.transaction(connection):
                Database.execute_script(connection, up_sql)
                Database.execute(
                    connection,
                    "INSERT INTO migrations (name) VALUES (?)",
                    (name,),
                )
            cls._migrations_applied.add(name)
        except Exception:
            logger.exception("Migration failed: %s", name)
            raise

    @classmethod
    def revert_migration(cls, name: str, down_sql: str) -> None:
        cls.init_migration_table()
        if name not in cls._migrations_applied:
            return
        connection = Database.get_connection()
        try:
            with Database.transaction(connection):
                Database.execute_script(connection, down_sql)
                Database.execute(
                    connection,
                    "DELETE FROM migrations WHERE name = ?",
                    (name,),
                )
            cls._migrations_applied.discard(name)
        except Exception:
            logger.exception("Migration rollback failed: %s", name)
            raise


def _annotation_fields(cls: Type[Any]) -> Dict[str, Any]:
    try:
        return get_type_hints(cls)
    except (NameError, TypeError):
        return dict(getattr(cls, "__annotations__", {}))


def _sql_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid SQL identifier: {name}")
    return name


def _sqlite_type(type_hint: Any) -> str:
    origin = get_origin(type_hint)
    if origin is Union:
        type_hint = next((arg for arg in get_args(type_hint) if arg is not type(None)), str)
    if type_hint is int or type_hint is bool:
        return "INTEGER"
    if type_hint is float:
        return "REAL"
    if type_hint is bytes:
        return "BLOB"
    return "TEXT"


class Model:
    """Small annotated model helper for SQLite and PostgreSQL."""

    def __init__(self, **kwargs: Any):
        for key, value in kwargs.items():
            setattr(self, key, value)

    @classmethod
    def get_table_name(cls) -> str:
        return cls.__name__.lower() + "s"

    @classmethod
    def create_table(cls) -> None:
        fields = _annotation_fields(cls)
        if not fields:
            raise ValueError(f"{cls.__name__} must declare annotated fields")
        sql_fields = []
        connection = Database.get_connection()
        for name, type_hint in fields.items():
            _sql_identifier(name)
            if name == "id":
                if Database.is_postgresql(connection):
                    sql_fields.append(
                        "id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
                    )
                else:
                    sql_fields.append("id INTEGER PRIMARY KEY AUTOINCREMENT")
            else:
                sql_fields.append(f"{name} {_database_type(connection, type_hint)}")
        with Database.transaction(connection):
            Database.execute(
                connection,
                f"CREATE TABLE IF NOT EXISTS {_sql_identifier(cls.get_table_name())} "
                f"({', '.join(sql_fields)})",
            )

    @classmethod
    def all(cls) -> List[sqlite3.Row]:
        connection = Database.get_connection()
        return Database.execute(
            connection,
            f"SELECT * FROM {_sql_identifier(cls.get_table_name())}",
        ).fetchall()

    @classmethod
    def get(cls, id: int) -> Optional[sqlite3.Row]:
        connection = Database.get_connection()
        return Database.execute(
            connection,
            f"SELECT * FROM {_sql_identifier(cls.get_table_name())} WHERE id = ?",
            (id,),
        ).fetchone()

    def save(self: "Model") -> "Model":
        fields = _annotation_fields(type(self))
        names = [name for name in fields if name != "id" and hasattr(self, name)]
        if not names:
            raise ValueError("Cannot save a model without fields")
        values = [getattr(self, name) for name in names]
        table = _sql_identifier(type(self).get_table_name())
        connection = Database.get_connection()
        with Database.transaction(connection):
            if getattr(self, "id", None):
                set_clause = ", ".join(f"{_sql_identifier(name)} = ?" for name in names)
                Database.execute(
                    connection,
                    f"UPDATE {table} SET {set_clause} WHERE id = ?",
                    tuple(values + [self.id]),
                )
            else:
                placeholders = ", ".join("?" for _ in names)
                insert_sql = (
                    f"INSERT INTO {table} ({', '.join(names)}) "
                    f"VALUES ({placeholders})"
                )
                if "id" in fields and Database.is_postgresql(connection):
                    cursor = Database.execute(
                        connection,
                        insert_sql + " RETURNING id",
                        tuple(values),
                    )
                    row = cursor.fetchone()
                    self.id = _row_value(row, "id")
                else:
                    cursor = Database.execute(connection, insert_sql, tuple(values))
                    self.id = cursor.lastrowid
        return self


class BaseModel:
    """Minimal request/response model with strict, predictable coercion."""

    def dict(self) -> Dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}

    @classmethod
    def validate(cls, data: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, Mapping):
            raise ValueError("Expected a JSON object")
        fields = _annotation_fields(cls)
        errors: Dict[str, str] = {}
        validated: Dict[str, Any] = {}
        for field, type_hint in fields.items():
            if field in data:
                try:
                    validated[field] = _coerce_value(data[field], type_hint)
                except (TypeError, ValueError) as exc:
                    errors[field] = str(exc)
            elif not hasattr(cls, field):
                errors[field] = "Field is required"
        if errors:
            raise ValueError(_json_dumps({"fields": errors}))
        return validated

    @classmethod
    def parse_obj(cls, data: Mapping[str, Any]) -> "BaseModel":
        instance = cls()
        for key, value in cls.validate(data).items():
            setattr(instance, key, value)
        return instance


def _coerce_value(value: Any, type_hint: Any) -> Any:
    origin = get_origin(type_hint)
    args = get_args(type_hint)
    if origin is Union:
        if value is None and type(None) in args:
            return None
        for candidate in args:
            if candidate is type(None):
                continue
            try:
                return _coerce_value(value, candidate)
            except (TypeError, ValueError):
                continue
        raise ValueError(f"Invalid value for {type_hint}")
    if value is None:
        raise ValueError("Value cannot be null")
    if type_hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "1", "yes", "on"}:
            return True
        if isinstance(value, str) and value.lower() in {"false", "0", "no", "off"}:
            return False
        raise ValueError("Expected a boolean")
    if type_hint is Any:
        return value
    if type_hint in (str, int, float):
        if type_hint is str and not isinstance(value, (str, int, float, bool)):
            raise TypeError("Expected a string")
        try:
            return type_hint(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid value: expected {type_hint.__name__}") from None
    if inspect.isclass(type_hint) and issubclass(type_hint, BaseModel):
        return type_hint.parse_obj(value)
    if inspect.isclass(type_hint) and issubclass(type_hint, Enum):
        return type_hint(value)
    return value


class ParamTypes(Enum):
    QUERY = "query"
    PATH = "path"
    BODY = "body"


@dataclass
class Param:
    name: str
    type: Type[Any]
    default: Any = None
    required: bool = True
    param_type: ParamTypes = ParamTypes.QUERY


@dataclass
class Query:
    default: Any = None
    description: str = ""


@dataclass
class Path:
    description: str = ""


class Role(Enum):
    ADMIN = "admin"
    USER = "user"
    GUEST = "guest"
    MODERATOR = "moderator"


class User(Model):
    """Convenience user model retained from the original Vedrakit API."""

    id: int
    username: str
    email: str
    password_hash: str
    role: str = Role.USER.value

    @classmethod
    def create(
        cls,
        username: str,
        email: str,
        password: str,
        role: Role = Role.USER,
    ) -> "User":
        user = cls(
            username=username,
            email=email,
            password_hash=Security.hash_password(password),
            role=role.value,
        )
        return user.save()

    def verify_password(self, password: str) -> bool:
        return Security.verify_password(password, self.password_hash)


class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: str = Role.USER.value


class UserResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str


class Security:
    _rate_limit_store: Dict[str, tuple[int, float]] = {}
    _rate_limit_lock = threading.Lock()

    @staticmethod
    def hash_password(password: str) -> str:
        if not isinstance(password, str) or len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        iterations = 310_000
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return f"pbkdf2_sha256${iterations}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        if not isinstance(password, str) or not isinstance(hashed, str):
            return False
        try:
            if hashed.startswith("pbkdf2_sha256$"):
                _, iterations, salt_text, digest_text = hashed.split("$", 3)
                salt = base64.urlsafe_b64decode(salt_text.encode())
                expected = base64.urlsafe_b64decode(digest_text.encode())
                actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
                return hmac.compare_digest(actual, expected)
            # Read old prototype hashes so existing users can migrate on login.
            if "$" in hashed:
                salt, expected = hashed.split("$", 1)
                actual = hashlib.sha256((salt + password).encode()).hexdigest()
                return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False
        return False

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @classmethod
    def create_jwt(cls, payload: Mapping[str, Any], expires_minutes: Optional[int] = None) -> str:
        secret = Config.JWT_SECRET
        if not secret:
            raise RuntimeError("JWT_SECRET must be configured before issuing tokens")
        now = datetime.now(timezone.utc)
        claims = dict(payload)
        claims.setdefault("iat", int(now.timestamp()))
        claims["exp"] = int((now + timedelta(minutes=expires_minutes or Config.JWT_EXPIRE_MINUTES)).timestamp())
        header = {"typ": "JWT", "alg": Config.JWT_ALGORITHM}
        encoded_header = cls._b64(_json_dumps(header).encode())
        encoded_claims = cls._b64(_json_dumps(claims).encode())
        signing_input = f"{encoded_header}.{encoded_claims}".encode()
        signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        return f"{encoded_header}.{encoded_claims}.{cls._b64(signature)}"

    @classmethod
    def verify_jwt(cls, token: str) -> Dict[str, Any]:
        if not token or not cls._rate_limited_format(token):
            raise ValueError("Invalid token")
        try:
            header_text, claims_text, signature_text = token.split(".")
            header = json.loads(cls._unb64(header_text))
            claims = json.loads(cls._unb64(claims_text))
            if header.get("alg") != Config.JWT_ALGORITHM:
                raise ValueError("Invalid token algorithm")
            expected = hmac.new(
                Config.JWT_SECRET.encode(),
                f"{header_text}.{claims_text}".encode(),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(expected, cls._unb64(signature_text)):
                raise ValueError("Invalid token signature")
            if "exp" not in claims or int(claims["exp"]) <= int(datetime.now(timezone.utc).timestamp()):
                raise ValueError("Token has expired")
            return claims
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise ValueError("Invalid token") from None

    @staticmethod
    def _rate_limited_format(token: str) -> bool:
        return len(token) <= 8192 and token.count(".") == 2

    @classmethod
    def check_rate_limit(cls, client_ip: str, endpoint: str) -> bool:
        key = f"{client_ip}:{endpoint}"
        now = time.monotonic()
        if Config.REDIS_URL and _redis is not None:
            try:
                redis_conn = RedisManager.get_connection()
                if redis_conn is not None:
                    current = redis_conn.incr(f"ratelimit:{key}")
                    if current == 1:
                        redis_conn.expire(f"ratelimit:{key}", Config.RATE_LIMIT_WINDOW)
                    return current <= Config.RATE_LIMIT_REQUESTS
            except Exception:
                logger.warning("Redis rate limiting unavailable; using in-memory fallback")
        with cls._rate_limit_lock:
            count, started = cls._rate_limit_store.get(key, (0, now))
            if now - started >= Config.RATE_LIMIT_WINDOW:
                count, started = 0, now
            count += 1
            cls._rate_limit_store[key] = (count, started)
            return count <= Config.RATE_LIMIT_REQUESTS


class RedisManager:
    _connection: Any = None

    @classmethod
    def get_connection(cls) -> Any:
        if not Config.REDIS_URL or _redis is None:
            return None
        if cls._connection is None:
            cls._connection = _redis.from_url(Config.REDIS_URL, socket_connect_timeout=1)
        return cls._connection

    @classmethod
    def close_connection(cls) -> None:
        if cls._connection is not None:
            close = getattr(cls._connection, "close", None)
            if close:
                close()
            cls._connection = None


def cache(timeout: int = 60) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Cache JSON-serializable return values in Redis or a process-local store."""
    memory: Dict[str, tuple[float, Any]] = {}
    lock = threading.Lock()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        def make_key(args: tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
            raw = _json_dumps([func.__module__, func.__qualname__, args, sorted(kwargs.items())])
            return "cache:" + hashlib.sha256(raw.encode()).hexdigest()

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            key = make_key(args, kwargs)
            cached = _cache_get(key)
            if cached is not None:
                return cached
            result = await func(*args, **kwargs)
            _cache_set(key, result)
            return result

        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            key = make_key(args, kwargs)
            cached = _cache_get(key)
            if cached is not None:
                return cached
            result = func(*args, **kwargs)
            _cache_set(key, result)
            return result

        def _cache_get(key: str) -> Any:
            redis_conn = RedisManager.get_connection()
            if redis_conn is not None:
                try:
                    value = redis_conn.get(key)
                    return json.loads(value) if value else None
                except Exception:
                    logger.warning("Redis cache read failed; using memory cache")
            with lock:
                item = memory.get(key)
                if item and item[0] > time.monotonic():
                    return item[1]
                memory.pop(key, None)
            return None

        def _cache_set(key: str, value: Any) -> None:
            try:
                serialized = _json_dumps(value)
            except TypeError:
                logger.warning("Skipping cache for non-JSON response from %s", func.__qualname__)
                return
            redis_conn = RedisManager.get_connection()
            if redis_conn is not None:
                try:
                    redis_conn.setex(key, timeout, serialized)
                    return
                except Exception:
                    logger.warning("Redis cache write failed; using memory cache")
            with lock:
                memory[key] = (time.monotonic() + timeout, value)

        wrapper = async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper
        return wraps(func)(wrapper)

    return decorator


class _Counter:
    def __init__(self, name: str, help_text: str):
        self.name, self.help_text = name, help_text
        self._values: Dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def labels(self, **labels: Any) -> "_MetricValue":
        key = tuple(sorted((name, str(value)) for name, value in labels.items()))
        return _MetricValue(self, key)

    def inc(self, amount: float = 1, **labels: Any) -> None:
        self.labels(**labels).inc(amount)


class _MetricValue:
    def __init__(self, metric: _Counter, key: tuple[tuple[str, str], ...]):
        self.metric, self.key = metric, key

    def inc(self, amount: float = 1) -> None:
        with self.metric._lock:
            self.metric._values[self.key] = self.metric._values.get(self.key, 0) + amount

    def observe(self, amount: float) -> None:
        self.inc(amount)


class _Histogram(_Counter):
    def observe(self, amount: float, **labels: Any) -> None:
        self.inc(amount, **labels)


REQUEST_COUNT = _Counter("http_requests_total", "Total HTTP requests")
REQUEST_LATENCY = _Histogram("http_request_duration_seconds", "HTTP request duration")
ACTIVE_REQUESTS = _Counter("http_requests_active", "Active HTTP requests")


def generate_latest() -> bytes:
    lines: List[str] = []
    for metric in (REQUEST_COUNT, REQUEST_LATENCY, ACTIVE_REQUESTS):
        lines.append(f"# HELP {metric.name} {metric.help_text}")
        lines.append(f"# TYPE {metric.name} {'histogram' if isinstance(metric, _Histogram) else 'counter'}")
        for key, value in metric._values.items():
            label_text = ""
            if key:
                label_text = "{" + ",".join(f'{k}="{v}"' for k, v in key) + "}"
            lines.append(f"{metric.name}{label_text} {value}")
    return ("\n".join(lines) + "\n").encode()


class WebSocketManager:
    _connections: Dict[str, set[Any]] = {}
    _lock = threading.Lock()

    @classmethod
    def add_connection(cls, path: str, websocket: Any) -> None:
        with cls._lock:
            cls._connections.setdefault(path, set()).add(websocket)

    @classmethod
    def remove_connection(cls, path: str, websocket: Any) -> None:
        with cls._lock:
            connections = cls._connections.get(path)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    cls._connections.pop(path, None)

    @classmethod
    async def broadcast(cls, path: str, message: str) -> None:
        with cls._lock:
            connections = list(cls._connections.get(path, set()))
        for websocket in connections:
            try:
                await websocket.send(message)
            except Exception:
                cls.remove_connection(path, websocket)


class TaskQueue:
    """A thread-backed queue that safely accepts sync and async callables."""

    def __init__(self, name: str = "default"):
        self.name = name
        self.tasks: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], Dict[str, Any]]] = queue.Queue()
        self.workers: List[threading.Thread] = []
        self._stopping = threading.Event()
        task_queues[name] = self

    async def add_task(self, task_func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self.tasks.put((task_func, args, kwargs))

    def _run_task(self, task_func: Callable[..., Any], args: tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        result = task_func(*args, **kwargs)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def worker(self, worker_id: int) -> None:
        while not self._stopping.is_set():
            try:
                task_func, args, kwargs = self.tasks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._run_task(task_func, args, kwargs)
            except Exception:
                logger.exception("Task failed in queue %s (worker %s)", self.name, worker_id)
            finally:
                self.tasks.task_done()

    def start_workers(self, num_workers: int = 3) -> None:
        if num_workers < 1:
            raise ValueError("num_workers must be at least 1")
        for worker_id in range(len(self.workers) + 1, len(self.workers) + num_workers + 1):
            worker = threading.Thread(target=self.worker, args=(worker_id,), daemon=True)
            worker.start()
            self.workers.append(worker)

    def stop(self) -> None:
        self._stopping.set()
        for worker in self.workers:
            worker.join(timeout=1)
        self.workers.clear()
        task_queues.pop(self.name, None)


task_queues: Dict[str, TaskQueue] = {}
websocket_handlers: Dict[str, Callable[..., Awaitable[Any]]] = {}
graphql_schemas: Dict[str, Any] = {}


def _path_from_request(path: str) -> str:
    return urllib.parse.urlparse(path).path or "/"


def extract_params(func: Callable[..., Any], route_path: str) -> List[Param]:
    params: List[Param] = []
    try:
        hints = get_type_hints(func)
    except (NameError, TypeError):
        hints = {}
    path_names = set(re.findall(r"\{(\w+)\}", route_path))
    for name, parameter in inspect.signature(func).parameters.items():
        type_hint = hints.get(name, str)
        default = parameter.default
        required = default is inspect.Parameter.empty
        if name in path_names:
            location = ParamTypes.PATH
        elif inspect.isclass(type_hint) and issubclass(type_hint, BaseModel):
            location = ParamTypes.BODY
        elif isinstance(default, Query):
            location = ParamTypes.QUERY
            default = default.default
            required = default is None and parameter.default.default is None
        else:
            location = ParamTypes.QUERY
        params.append(Param(name, type_hint, default, required, location))
    return params


@dataclass
class Route:
    path: str
    methods: tuple[str, ...]
    func: Callable[..., Any]
    response_model: Optional[Type[Any]]
    require_auth: bool
    required_roles: List[Role]
    params: List[Param]


class App:
    def __init__(self, static_dir: str = "static"):
        self.routes: Dict[tuple[str, tuple[str, ...]], Route] = {}
        self.middlewares: List[Callable[[Any], bool]] = []
        self.exception_handlers: Dict[Type[Exception], Callable[[Exception], Any]] = {}
        self.static_dir = static_dir
        self.websocket_handlers = websocket_handlers
        self.graphql_schemas = graphql_schemas

    def route(
        self,
        path: str,
        methods: List[str],
        response_model: Optional[Type[Any]] = None,
        require_auth: bool = False,
        required_roles: Optional[List[Role]] = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            normalized = path if path.startswith("/") else "/" + path
            method_tuple = tuple(method.upper() for method in methods)
            roles = list(required_roles or [])
            declared_role = getattr(func, "required_role", None)
            if declared_role is not None and declared_role not in roles:
                roles.append(declared_role)
            self.routes[(normalized, method_tuple)] = Route(
                normalized,
                method_tuple,
                func,
                response_model,
                require_auth,
                roles,
                extract_params(func, normalized),
            )
            return func

        return decorator

    def middleware(self, func: Callable[[Any], bool]) -> Callable[[Any], bool]:
        self.middlewares.append(func)
        return func

    def exception_handler(self, exc_type: Type[Exception]) -> Callable[[Callable[[Exception], Any]], Callable[[Exception], Any]]:
        def decorator(func: Callable[[Exception], Any]) -> Callable[[Exception], Any]:
            self.exception_handlers[exc_type] = func
            return func

        return decorator

    def _find_route(self, method: str, path: str) -> tuple[Optional[Route], Dict[str, str], bool]:
        clean_path = _path_from_request(path)
        method_allowed = False
        for route in self.routes.values():
            pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", route.path)
            if re.fullmatch(pattern, clean_path):
                method_allowed = True
                if method in route.methods:
                    return route, {k: urllib.parse.unquote(v) for k, v in re.match(pattern, clean_path).groupdict().items()}, True
        return None, {}, method_allowed

    def _parse_request_body(self, handler: "RequestHandler") -> Dict[str, Any]:
        length_text = handler.headers.get("Content-Length", "0")
        try:
            content_length = int(length_text)
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if content_length < 0 or content_length > 10 * 1024 * 1024:
            raise ValueError("Request body is too large")
        if content_length == 0:
            return {}
        body = handler.rfile.read(content_length)
        content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            try:
                value = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("Invalid JSON in request body") from None
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value
        if content_type == "application/x-www-form-urlencoded":
            return {key: values[-1] for key, values in urllib.parse.parse_qs(body.decode()).items()}
        return {"raw_body": body}

    def _dependency_targets(self, route: Route) -> set[str]:
        dependencies = getattr(route.func, "dependencies", [])
        if not dependencies:
            return set()
        parameter_names = {parameter.name for parameter in route.params}
        targets: set[str] = set()
        candidates = [
            parameter.name
            for parameter in route.params
            if parameter.param_type == ParamTypes.QUERY and parameter.required
        ]
        for dependency in dependencies:
            if dependency.__name__ in parameter_names:
                targets.add(dependency.__name__)
            elif len(candidates) == 1:
                targets.add(candidates[0])
        return targets

    def _parse_params(
        self,
        handler: "RequestHandler",
        route: Route,
        path_params: Dict[str, str],
        dependency_targets: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = dict(path_params)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query, keep_blank_values=True)
        params.update({key: values[-1] if len(values) == 1 else values for key, values in query.items()})
        body: Dict[str, Any] = {}
        if handler.command in {"POST", "PUT", "PATCH"}:
            body = self._parse_request_body(handler)
            params.update(body)
        result: Dict[str, Any] = {}
        for definition in route.params:
            if dependency_targets and definition.name in dependency_targets:
                continue
            if definition.param_type == ParamTypes.BODY:
                if not body:
                    if definition.required:
                        raise ValueError(f"Missing required parameter: {definition.name}")
                    result[definition.name] = definition.default
                else:
                    result[definition.name] = definition.type.parse_obj(body)
                continue
            if definition.name in params:
                value = params[definition.name]
                if inspect.isclass(definition.type) and issubclass(definition.type, BaseModel):
                    result[definition.name] = definition.type.parse_obj(value)
                else:
                    result[definition.name] = _coerce_value(value, definition.type)
            elif definition.required:
                raise ValueError(f"Missing required parameter: {definition.name}")
            else:
                result[definition.name] = definition.default
        return result

    def _resolve_dependencies(self, handler: Callable[..., Any], request: "RequestHandler", params: Dict[str, Any]) -> Dict[str, Any]:
        resolved = dict(params)
        handler_parameters = inspect.signature(handler).parameters
        for dependency in getattr(handler, "dependencies", []):
            dependency_params: Dict[str, Any] = {}
            for name in inspect.signature(dependency).parameters:
                if name in resolved:
                    dependency_params[name] = resolved[name]
                elif name in {"req", "request", "handler"}:
                    dependency_params[name] = request
            value = dependency(**dependency_params)
            if inspect.isawaitable(value):
                value = asyncio.run(value)
            if dependency.__name__ in handler_parameters:
                resolved[dependency.__name__] = value
            missing = [name for name in handler_parameters if name not in resolved]
            if len(missing) == 1:
                resolved[missing[0]] = value
        return resolved

    def _execute(self, func: Callable[..., Any], params: Dict[str, Any]) -> Any:
        result = func(**params)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def _validate_response(self, result: Any, model: Type[Any]) -> Any:
        if isinstance(result, tuple) and len(result) == 2:
            status, data = result
            if int(status) >= 400:
                return result
            if isinstance(data, Mapping) and issubclass(model, BaseModel):
                return status, model.validate(data)
            return result
        if isinstance(result, Mapping) and issubclass(model, BaseModel):
            return model.validate(result)
        return result

    def openapi(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "openapi": "3.0.3",
            "info": {"title": "Vedrakit API", "version": "1.0.0"},
            "paths": {},
            "components": {"schemas": {}},
        }
        for route in self.routes.values():
            path_data = document["paths"].setdefault(route.path, {})
            for method in route.methods:
                operation: Dict[str, Any] = {
                    "summary": inspect.getdoc(route.func) or "",
                    "responses": {"200": {"description": "Successful response"}},
                }
                parameters = []
                body_model = None
                for param in route.params:
                    if param.param_type == ParamTypes.BODY:
                        body_model = param.type
                    else:
                        parameters.append(
                            {
                                "name": param.name,
                                "in": param.param_type.value,
                                "required": param.required,
                                "schema": {"type": _openapi_type(param.type)},
                            }
                        )
                if parameters:
                    operation["parameters"] = parameters
                if body_model:
                    operation["requestBody"] = {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{body_model.__name__}"}}},
                    }
                    document["components"]["schemas"][body_model.__name__] = _model_schema(body_model)
                if route.response_model and inspect.isclass(route.response_model) and issubclass(route.response_model, BaseModel):
                    document["components"]["schemas"][route.response_model.__name__] = _model_schema(route.response_model)
                path_data[method.lower()] = operation
        return document

    def serve_static(self, handler: "RequestHandler") -> None:
        relative = urllib.parse.unquote(_path_from_request(handler.path)[len("/static/") :])
        root = os.path.realpath(self.static_dir)
        target = os.path.realpath(os.path.join(root, relative))
        if not (target == root or target.startswith(root + os.sep)) or not os.path.isfile(target):
            handler._send_response(404, "Not Found", "text/plain")
            return
        mime_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as file:
            handler._send_response(200, file.read(), mime_type)

    def check_readiness(self) -> bool:
        try:
            Database.get_connection().execute("SELECT 1")
            return True
        except Exception:
            return False

    def serve_metrics(self, handler: "RequestHandler") -> None:
        handler._send_response(200, generate_latest(), "text/plain; version=0.0.4")

    def handle_graphql(self, handler: "RequestHandler") -> None:
        body = self._parse_request_body(handler)
        query = body.get("query")
        if not query:
            handler._send_json_response(400, {"error": "No query provided"})
            return
        if not self.graphql_schemas:
            handler._send_json_response(501, {"error": "GraphQL not configured"})
            return
        schema = next(iter(self.graphql_schemas.values()))
        result = schema.execute(query, variables=body.get("variables"), operation_name=body.get("operationName"))
        response: Dict[str, Any] = {"data": result.data}
        if result.errors:
            response["errors"] = [{"message": str(error)} for error in result.errors]
        handler._send_json_response(200, response)

    def make_server(self, host: str, port: int, production: bool = True) -> http.server.HTTPServer:
        if production:
            from socketserver import ThreadingMixIn

            class ThreadedHTTPServer(ThreadingMixIn, http.server.HTTPServer):
                daemon_threads = True

            server = ThreadedHTTPServer((host, port), RequestHandler)
        else:
            server = http.server.HTTPServer((host, port), RequestHandler)
        server.app = self  # type: ignore[attr-defined]
        return server

    def run(self, port: Optional[int] = None, production: bool = False, host: Optional[str] = None, start_workers: int = 3) -> None:
        if production:
            Config.validate_production()
        port = port or int(os.environ.get("PORT", "8080"))
        host = host or ("0.0.0.0" if production else "127.0.0.1")
        os.makedirs(self.static_dir, exist_ok=True)
        Migration.init_migration_table()
        default_queue = task_queues.get("default") or TaskQueue("default")
        default_queue.start_workers(start_workers)
        server = self.make_server(host, port, production)
        logger.info("Vedrakit listening on %s:%s", host, port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down Vedrakit")
        finally:
            server.server_close()
            default_queue.stop()
            Database.close_all()
            RedisManager.close_connection()


def _openapi_type(type_hint: Any) -> str:
    origin = get_origin(type_hint)
    if origin is Union:
        type_hint = next((arg for arg in get_args(type_hint) if arg is not type(None)), str)
    return {int: "integer", float: "number", bool: "boolean", bytes: "string"}.get(type_hint, "string")


def _model_schema(model: Type[Any]) -> Dict[str, Any]:
    fields = _annotation_fields(model)
    required = [name for name in fields if not hasattr(model, name)]
    return {
        "type": "object",
        "properties": {name: {"type": _openapi_type(type_hint)} for name, type_hint in fields.items()},
        "required": required,
    }


class RequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "Vedrakit/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    @property
    def application(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def _set_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = Config.CORS_ORIGINS
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if origin and (origin in allowed or "*" in allowed):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._handle_request()

    def do_POST(self) -> None:
        self._handle_request()

    def do_PUT(self) -> None:
        self._handle_request()

    def do_PATCH(self) -> None:
        self._handle_request()

    def do_DELETE(self) -> None:
        self._handle_request()

    def _handle_request(self) -> None:
        started = time.monotonic()
        ACTIVE_REQUESTS.inc()
        status = 500
        endpoint = _path_from_request(self.path)
        try:
            client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
            if not Security.check_rate_limit(client_ip, endpoint):
                self._send_json_response(429, {"error": "Rate limit exceeded"})
                status = 429
                return
            for middleware_func in self.application.middlewares:
                if not middleware_func(self):
                    self._send_json_response(403, {"error": "Forbidden by middleware"})
                    status = 403
                    return
            if endpoint.startswith("/static/"):
                self.application.serve_static(self)
                status = 200
                return
            if endpoint == "/docs":
                self._send_json_response(200, self.application.openapi())
                status = 200
                return
            if endpoint == "/metrics":
                self.application.serve_metrics(self)
                status = 200
                return
            if endpoint == "/health":
                self._send_json_response(200, {"status": "healthy"})
                status = 200
                return
            if endpoint == "/ready":
                status = 200 if self.application.check_readiness() else 503
                self._send_json_response(status, {"status": "ready" if status == 200 else "not ready"})
                return
            if endpoint == "/graphql" and self.command == "POST":
                self.application.handle_graphql(self)
                status = 200
                return
            route, path_params, method_allowed = self.application._find_route(self.command, self.path)
            if not route:
                status = 405 if method_allowed else 404
                if method_allowed:
                    self.send_response(405)
                    self.send_header("Allow", ", ".join(sorted({m for r in self.application.routes.values() if _path_from_request(r.path) == endpoint for m in r.methods})))
                    self._set_cors_headers()
                    self.end_headers()
                else:
                    self._send_json_response(404, {"error": "Not Found"})
                return
            if route.require_auth:
                authorized, auth_status, auth_body = self._check_auth(route.required_roles)
                if not authorized:
                    self._send_json_response(auth_status, auth_body)
                    status = auth_status
                    return
            params = self.application._parse_params(
                self, route, path_params, self.application._dependency_targets(route)
            )
            params = self.application._resolve_dependencies(route.func, self, params)
            result = self.application._execute(route.func, params)
            if route.response_model:
                result = self.application._validate_response(result, route.response_model)
            self._send_appropriate_response(result)
            status = result[0] if isinstance(result, tuple) and len(result) == 2 else 200
        except Exception as exc:
            status = self._handle_exception(exc)
        finally:
            ACTIVE_REQUESTS.inc(-1)
            REQUEST_COUNT.labels(method=self.command, endpoint=endpoint, status=status).inc()
            REQUEST_LATENCY.labels(endpoint=endpoint).observe(time.monotonic() - started)

    def _check_auth(self, required_roles: List[Role]) -> tuple[bool, int, Dict[str, Any]]:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False, 401, {"error": "Authentication required"}
        try:
            payload = Security.verify_jwt(header[7:].strip())
            self.request_context = {"user": payload}
            if required_roles and payload.get("role") not in {role.value for role in required_roles}:
                return False, 403, {"error": "Insufficient permissions"}
            return True, 200, {}
        except ValueError as exc:
            return False, 401, {"error": str(exc)}

    def _handle_exception(self, exc: Exception) -> int:
        if isinstance(exc, (ValueError, TypeError)):
            logger.warning("Client request rejected: %s", exc)
        else:
            logger.exception("Request failed: %s", exc)
        for exc_type, callback in self.application.exception_handlers.items():
            if isinstance(exc, exc_type):
                self._send_appropriate_response(callback(exc))
                return 400 if isinstance(exc, (ValueError, TypeError)) else 500
        if isinstance(exc, (ValueError, TypeError)):
            self._send_json_response(400, {"error": str(exc)})
            return 400
        self._send_json_response(500, {"error": "Internal Server Error"})
        return 500

    def _send_appropriate_response(self, result: Any) -> None:
        if isinstance(result, tuple) and len(result) == 2:
            status, content = result
            if isinstance(content, Mapping):
                self._send_json_response(int(status), dict(content))
            else:
                self._send_response(int(status), str(content), "text/plain")
        elif isinstance(result, Mapping) or is_dataclass(result):
            self._send_json_response(200, result)
        else:
            self._send_response(200, str(result), "text/plain")

    def _send_json_response(self, code: int, data: Any) -> None:
        content = _json_dumps(data).encode()
        self.send_response(code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_response(self, code: int, content: Union[str, bytes], content_type: str) -> None:
        body = content.encode() if isinstance(content, str) else content
        self.send_response(code)
        self._set_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def route(path: str, methods: List[str], response_model: Optional[Type[Any]] = None, require_auth: bool = False, required_roles: Optional[List[Role]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return app.route(path, methods, response_model, require_auth, required_roles)


def get(path: str, response_model: Optional[Type[Any]] = None, require_auth: bool = False, required_roles: Optional[List[Role]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route(path, ["GET"], response_model, require_auth, required_roles)


def post(path: str, response_model: Optional[Type[Any]] = None, require_auth: bool = False, required_roles: Optional[List[Role]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route(path, ["POST"], response_model, require_auth, required_roles)


def put(path: str, response_model: Optional[Type[Any]] = None, require_auth: bool = False, required_roles: Optional[List[Role]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route(path, ["PUT"], response_model, require_auth, required_roles)


def delete(path: str, response_model: Optional[Type[Any]] = None, require_auth: bool = False, required_roles: Optional[List[Role]] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    return route(path, ["DELETE"], response_model, require_auth, required_roles)


def middleware(func: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return app.middleware(func)


def exception_handler(exc_type: Type[Exception]) -> Callable[[Callable[[Exception], Any]], Callable[[Exception], Any]]:
    return app.exception_handler(exc_type)


def depends(dependency: Callable[..., Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.dependencies = getattr(func, "dependencies", [])
        func.dependencies.append(dependency)
        return func

    return decorator


def requires_role(role: Role) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.required_role = role
        return func

    return decorator


def get_current_user(req: RequestHandler) -> Dict[str, Any]:
    """Dependency returning the authenticated JWT payload."""
    context = getattr(req, "request_context", {})
    if "user" not in context:
        raise ValueError("User not authenticated")
    return context["user"]


def background_task(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        default_queue = task_queues.get("default")
        if default_queue and default_queue.workers:
            default_queue.tasks.put((func, args, kwargs))
        else:
            threading.Thread(target=lambda: _run_background(func, args, kwargs), daemon=True).start()

    return wrapper


def _run_background(func: Callable[..., Any], args: tuple[Any, ...], kwargs: Dict[str, Any]) -> None:
    try:
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            asyncio.run(result)
    except Exception:
        logger.exception("Background task %s failed", func.__name__)


def websocket_endpoint(path: str) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        websocket_handlers[path] = func
        return func

    return decorator


def graphql_schema(schema: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        graphql_schemas[func.__name__] = schema
        return func

    return decorator


app = App()
routes = app.routes
middlewares = app.middlewares
exception_handlers = app.exception_handlers


def run(port: Optional[int] = None, production: bool = False, host: Optional[str] = None) -> None:
    """Run the default decorated application."""
    app.run(port=port, production=production, host=host)


def run_websocket_server(host: str = "0.0.0.0", port: Optional[int] = None) -> None:
    """Run registered WebSocket routes as a standalone optional service."""
    if _websockets is None:
        raise RuntimeError("Install the websocket extra to use WebSocket support")
    actual_port = port or Config.WEBSOCKET_PORT

    async def dispatch(websocket: Any, path: Optional[str] = None) -> None:
        # websockets < 13 passes (websocket, path); newer releases expose the
        # path on the connection object and pass only the connection.
        path = path or getattr(websocket, "path", "/")
        handler = websocket_handlers.get(path)
        if handler is None:
            await websocket.close(code=1008, reason="Unknown endpoint")
            return
        await handler(websocket, path)

    async def serve() -> None:
        server = await _websockets.serve(dispatch, host, actual_port)
        logger.info("WebSocket server listening on %s:%s", host, actual_port)
        await server.wait_closed()

    asyncio.run(serve())


def run_grpc_server(
    servicer: Optional[Any] = None,
    add_servicer: Optional[Callable[[Any, Any], None]] = None,
    host: str = "0.0.0.0",
    port: Optional[int] = None,
    max_workers: int = 10,
) -> Any:
    """Start a real gRPC server when generated service bindings are supplied."""
    if _grpc is None:
        raise RuntimeError("Install the grpc extra to use gRPC support")
    server = _grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    if servicer is not None and add_servicer is not None:
        add_servicer(servicer, server)
    server.add_insecure_port(f"{host}:{port or Config.GRPC_PORT}")
    server.start()
    logger.info("gRPC server listening on %s:%s", host, port or Config.GRPC_PORT)
    return server


def run_metrics_server(host: str = "0.0.0.0", port: Optional[int] = None) -> Any:
    """Expose the built-in Prometheus-compatible metrics endpoint."""
    class MetricsHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("metrics - %s", format % args)

    server = http.server.ThreadingHTTPServer((host, port or Config.PROMETHEUS_PORT), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Metrics server listening on %s:%s", host, port or Config.PROMETHEUS_PORT)
    return server


# Compatibility name used by the prototype. New code should use RequestHandler.
AdvancedMiniFlask = RequestHandler
