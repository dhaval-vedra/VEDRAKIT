import json
import tempfile
import unittest
from pathlib import Path

from vedrakit import App, BaseModel, Query, Role, generate_typescript_client
from vedrakit.cli import main


class Address(BaseModel):
    city: str


class CreateUser(BaseModel):
    name: str
    address: Address
    active: bool = True


class UserResponse(BaseModel):
    id: int
    name: str


class ToolingTestCase(unittest.TestCase):
    def make_app(self):
        app = App(
            title="Users API",
            version="2.0.0",
            description="Manage users.",
            servers=[{"url": "https://api.example.test"}],
        )

        @app.post(
            "/users/{user_id}",
            response_model=UserResponse,
            require_auth=True,
            required_roles=[Role.ADMIN],
            summary="Create a user",
            tags=["users"],
            operation_id="createUser",
            response_description="User created",
        )
        def create_user(
            user_id: int,
            payload: CreateUser,
            limit: int = Query(default=10, description="Maximum results"),
        ):
            return {"id": user_id, "name": payload.name}

        return app

    def test_openapi_contains_metadata_nested_schemas_and_security(self):
        document = self.make_app().openapi()
        operation = document["paths"]["/users/{user_id}"]["post"]

        self.assertEqual(document["info"]["title"], "Users API")
        self.assertEqual(document["info"]["version"], "2.0.0")
        self.assertEqual(operation["operationId"], "createUser")
        self.assertEqual(operation["tags"], ["users"])
        self.assertEqual(operation["parameters"][0]["schema"], {"type": "integer"})
        self.assertEqual(operation["parameters"][1]["schema"]["default"], 10)
        self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"], {
            "$ref": "#/components/schemas/CreateUser"
        })
        self.assertIn("Address", document["components"]["schemas"])
        self.assertEqual(operation["security"], [{"bearerAuth": []}])
        self.assertEqual(operation["x-vedrakit-required-roles"], ["admin"])
        json.dumps(document)

    def test_typescript_client_contains_typed_models_and_operations(self):
        client = generate_typescript_client(self.make_app().openapi())
        self.assertIn("export interface CreateUser", client)
        self.assertIn("address: Address;", client)
        self.assertIn("export interface UserResponse", client)
        self.assertIn("createUser: async", client)
        self.assertIn("encodeURIComponent", client)
        self.assertIn("JSON.stringify(body)", client)

    def test_new_command_creates_complete_scaffold(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo-api"
            exit_code = main(["new", "demo-api", "--directory", str(project)])
            self.assertEqual(exit_code, 0)
            for filename in (
                "app.py",
                "README.md",
                "pyproject.toml",
                ".env.example",
                ".gitignore",
                "tests/test_app.py",
            ):
                self.assertTrue((project / filename).is_file(), filename)
            self.assertIn("vedrakit dev", (project / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()