"""The dependency-free Vedrakit command-line interface."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Optional

from .codegen import generate_typescript_client
from .core import App


SCAFFOLD_FILES = {
    "app.py": '''"""A new Vedrakit application."""

from vedrakit import App, BaseModel, Query


class Item(BaseModel):
    name: str
    quantity: int = 1


app = App(
    title="{title}",
    version="0.1.0",
    description="A Vedrakit API created with the project scaffold.",
)


@app.get(
    "/items",
    summary="List items",
    tags=["items"],
)
def list_items(limit: int = Query(default=20, description="Maximum number of items")):
    """Return example items."""
    return {{"items": [], "limit": limit}}


@app.post(
    "/items",
    response_model=Item,
    summary="Create an item",
    tags=["items"],
)
def create_item(item: Item):
    """Validate and echo an item."""
    return item.dict()


if __name__ == "__main__":
    app.run(port=8080, production=False)
''',
    "README.md": """# {title}

Generated with Vedrakit.

## Run

```bash
python -m pip install -e .
vedrakit dev app:app
```

Open:

- http://127.0.0.1:8080/docs
- http://127.0.0.1:8080/health

## Generate a typed client

```bash
vedrakit openapi app:app --output openapi.json
vedrakit client app:app --output api-client.ts
```
""",
    "pyproject.toml": """[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "0.1.0"
description = "A Vedrakit API."
requires-python = ">=3.10"
dependencies = ["vedrakit>=1.1.0"]

[tool.setuptools]
py-modules = ["app"]
""",
    ".env.example": """# Copy to .env or configure these in your deployment environment.
SECRET_KEY=replace-me
JWT_SECRET=replace-me
DATABASE_URL=sqlite:///app.db
CORS_ORIGINS=http://localhost:3000
""",
    ".gitignore": """__pycache__/
*.py[cod]
.venv/
.env
*.db
openapi.json
api-client.ts
dist/
build/
*.egg-info/
""",
    "tests/test_app.py": """import unittest

from app import app


class AppTest(unittest.TestCase):
    def test_openapi_contains_items(self):
        document = app.openapi()
        self.assertIn("/items", document["paths"])
        self.assertIn("get", document["paths"]["/items"])


if __name__ == "__main__":
    unittest.main()
""",
}


def _load_module(module_name: str) -> ModuleType:
    if module_name.endswith(".py") or Path(module_name).exists():
        source_path = Path(module_name).resolve()
        if not source_path.is_file():
            raise SystemExit(f"Application module not found: {module_name}")
        spec = importlib.util.spec_from_file_location("vedrakit_cli_app", source_path)
        if spec is None or spec.loader is None:
            raise SystemExit(f"Could not load application module: {module_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(module_name)


def load_app(target: str = "app:app") -> App:
    """Load an App from ``module:attribute`` or a Python file target."""
    if ":" not in target:
        target = f"{target}:app"
    module_name, attribute = target.split(":", 1)
    module = _load_module(module_name)
    application = getattr(module, attribute, None)
    if not isinstance(application, App):
        raise SystemExit(f"{target} must point to a vedrakit.App instance")
    return application


def _safe_project_name(name: str) -> str:
    package_name = re.sub(r"[^a-zA-Z0-9_]+", "-", name.strip()).strip("-").lower()
    package_name = package_name or "vedrakit-app"
    return package_name


def scaffold_project(name: str, directory: Optional[str] = None, force: bool = False) -> Path:
    """Create a runnable Vedrakit project and return its directory."""
    project_dir = Path(directory or name).expanduser()
    if project_dir.exists() and any(project_dir.iterdir()) and not force:
        raise SystemExit(
            f"Directory {project_dir} is not empty; use --force to overwrite scaffold files"
        )
    project_dir.mkdir(parents=True, exist_ok=True)
    title = name.replace("-", " ").replace("_", " ").strip().title() or "Vedrakit App"
    package_name = _safe_project_name(name)
    for filename, template in SCAFFOLD_FILES.items():
        destination = project_dir / filename
        if destination.exists() and not force:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            template.format(title=title, package_name=package_name),
            encoding="utf-8",
        )
    return project_dir


def _write_or_print(content: str, output: Optional[str]) -> None:
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        print(f"Wrote {destination}")
    else:
        print(content, end="" if content.endswith("\n") else "\n")


def _command_routes(application: App) -> None:
    print("METHOD\tPATH\tOPERATION ID\tAUTH\tHANDLER")
    for route in sorted(application.routes.values(), key=lambda item: (item.path, item.methods)):
        for method in route.methods:
            auth = "yes" if route.require_auth else "no"
            print(f"{method}\t{route.path}\t{route.operation_id}\t{auth}\t{route.func.__name__}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vedrakit",
        description="Build, inspect, and generate artifacts for Vedrakit APIs.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_parser = subparsers.add_parser("new", help="Create a runnable Vedrakit project")
    new_parser.add_argument("name", help="Project name")
    new_parser.add_argument("--directory", help="Directory to create")
    new_parser.add_argument("--force", action="store_true", help="Overwrite scaffold files")

    for command, help_text in (
        ("dev", "Run a Vedrakit application"),
        ("routes", "List registered routes"),
        ("openapi", "Generate an OpenAPI JSON document"),
        ("client", "Generate a typed TypeScript client"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("target", nargs="?", default="app:app", help="module:app target")
        if command == "dev":
            command_parser.add_argument("--host", default="127.0.0.1")
            command_parser.add_argument("--port", type=int, default=8080)
            command_parser.add_argument("--production", action="store_true")
        elif command == "openapi":
            command_parser.add_argument("--output", "-o")
            command_parser.add_argument("--indent", type=int, default=2)
        elif command == "client":
            command_parser.add_argument("--output", "-o", default="api-client.ts")
            command_parser.add_argument("--name", default="VedrakitClient")

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "new":
        project_dir = scaffold_project(args.name, args.directory, args.force)
        print(f"Created Vedrakit project in {project_dir}")
        return 0
    application = load_app(args.target)
    if args.command == "dev":
        application.run(
            host=args.host,
            port=args.port,
            production=args.production,
        )
        return 0
    if args.command == "routes":
        _command_routes(application)
        return 0
    if args.command == "openapi":
        content = json.dumps(application.openapi(), indent=args.indent, sort_keys=True) + "\n"
        _write_or_print(content, args.output)
        return 0
    if args.command == "client":
        content = generate_typescript_client(application.openapi(), client_name=args.name)
        _write_or_print(content, args.output)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())