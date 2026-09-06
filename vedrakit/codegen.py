"""Small, dependency-free client generators built on Vedrakit OpenAPI output."""

from __future__ import annotations

import json
import keyword
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional


def _schema_ref(schema: Mapping[str, Any]) -> Optional[str]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    return None


def _ts_property_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) else json.dumps(name)


def _ts_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return json.dumps(value)


def _typescript_type(schema: Mapping[str, Any]) -> str:
    ref = _schema_ref(schema)
    if ref:
        return ref
    if "oneOf" in schema or "anyOf" in schema:
        key = "oneOf" if "oneOf" in schema else "anyOf"
        values = [_typescript_type(item) for item in schema.get(key, [])]
        result = " | ".join(values) or "unknown"
        return f"({result})" if len(values) > 1 else result
    if "allOf" in schema:
        values = [_typescript_type(item) for item in schema["allOf"]]
        return " & ".join(values) or "unknown"
    if "enum" in schema:
        values = " | ".join(_ts_literal(value) for value in schema["enum"])
        result = values or "string"
    else:
        schema_type = schema.get("type")
        if schema_type == "array":
            item_type = _typescript_type(schema.get("items", {}))
            result = f"Array<{item_type}>"
        elif schema_type == "object":
            additional = schema.get("additionalProperties")
            if additional:
                result = f"Record<string, {_typescript_type(additional)}>"
            elif schema.get("properties"):
                fields = []
                required = set(schema.get("required", []))
                for name, property_schema in schema["properties"].items():
                    optional = "" if name in required else "?"
                    fields.append(
                        f"  {_ts_property_name(name)}{optional}: {_typescript_type(property_schema)};"
                    )
                result = "{\n" + "\n".join(fields) + "\n}"
            else:
                result = "Record<string, unknown>"
        else:
            result = {
                "integer": "number",
                "number": "number",
                "boolean": "boolean",
                "string": "string",
                "null": "null",
            }.get(schema_type, "unknown")
    if schema.get("nullable") and "null" not in result:
        result = f"{result} | null"
    return result


def _safe_identifier(value: str, fallback: str = "operation") -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    identifier = "".join(word[:1].upper() + word[1:] for word in words) or fallback
    identifier = identifier[:1].lower() + identifier[1:]
    if keyword.iskeyword(identifier):
        identifier += "Request"
    return identifier


def _operation_name(method: str, path: str, operation: Mapping[str, Any]) -> str:
    operation_id = operation.get("operationId")
    if operation_id:
        return _safe_identifier(str(operation_id))
    return _safe_identifier(f"{method}_{path.strip('/').replace('/', '_')}")


def _response_schema(operation: Mapping[str, Any]) -> Mapping[str, Any]:
    responses = operation.get("responses", {})
    for code in ("200", "201", "202", "default"):
        response = responses.get(code)
        if isinstance(response, Mapping):
            content = response.get("content", {})
            json_content = content.get("application/json", {})
            schema = json_content.get("schema")
            if isinstance(schema, Mapping):
                return schema
    return {}


def _path_expression(path: str, parameters: Iterable[Mapping[str, Any]]) -> str:
    parts = re.split(r"(\{[^}]+\})", path)
    rendered: List[str] = []
    parameter_names = {parameter["name"] for parameter in parameters if parameter.get("in") == "path"}
    for part in parts:
        match = re.fullmatch(r"\{([^}]+)\}", part)
        if match and match.group(1) in parameter_names:
            name = match.group(1)
            rendered.append(f"${{encodeURIComponent(String(params.{name}))}}")
        else:
            rendered.append(part.replace("`", "\\`"))
    return "`" + "".join(rendered) + "`"


def _query_expression(parameters: List[Mapping[str, Any]]) -> str:
    query_parameters = [parameter for parameter in parameters if parameter.get("in") == "query"]
    if not query_parameters:
        return ""
    lines = [
        "const query = new URLSearchParams();",
    ]
    for parameter in query_parameters:
        name = parameter["name"]
        if parameter.get("required"):
            lines.append(f"query.set({json.dumps(name)}, String(params.{name}));")
        else:
            lines.append(
                f"if (params.{name} !== undefined) query.set({json.dumps(name)}, String(params.{name}));"
            )
    lines.append("const queryString = query.toString();")
    lines.append("const requestPath = queryString ? `${path}?${queryString}` : path;")
    return "\n".join(lines)


def generate_typescript_client(
    document: Mapping[str, Any],
    *,
    client_name: str = "VedrakitClient",
) -> str:
    """Generate a fetch-based TypeScript client from an OpenAPI document."""
    schemas = document.get("components", {}).get("schemas", {})
    lines = [
        "/* Generated by Vedrakit. Do not edit by hand. */",
        "",
        "export type JsonObject = Record<string, unknown>;",
        "",
        "export class ApiError extends Error {",
        "  constructor(public readonly status: number, public readonly data: unknown) {",
        "    super(`API request failed with status ${status}`);",
        "    this.name = \"ApiError\";",
        "  }",
        "}",
        "",
    ]
    for name, schema in schemas.items():
        if schema.get("type") == "object" and schema.get("properties"):
            lines.append(f"export interface {name} {{")
            required = set(schema.get("required", []))
            for property_name, property_schema in schema["properties"].items():
                optional = "" if property_name in required else "?"
                lines.append(
                    f"  {_ts_property_name(property_name)}{optional}: {_typescript_type(property_schema)};"
                )
            lines.extend(["}", ""])
        else:
            lines.extend([f"export type {name} = {_typescript_type(schema)};", ""])

    lines.extend(
        [
            "export interface ApiClientOptions {",
            "  baseUrl?: string;",
            "  headers?: Record<string, string>;",
            "  fetch?: typeof fetch;",
            "}",
            "",
            "export function createApiClient(options: ApiClientOptions = {}) {",
            '  const baseUrl = (options.baseUrl ?? "").replace(/\\/$/, "");',
            "  const fetcher = options.fetch ?? fetch;",
            "  async function request<T>(path: string, init: RequestInit = {}): Promise<T> {",
            "    const response = await fetcher(`${baseUrl}${path}`, {",
            "      ...init,",
            "      headers: {",
            '        "Content-Type": "application/json",',
            "        ...options.headers,",
            "        ...(init.headers ?? {}),",
            "      },",
            "    });",
            "    const text = await response.text();",
            "    const data = text ? JSON.parse(text) : undefined;",
            "    if (!response.ok) throw new ApiError(response.status, data);",
            "    return data as T;",
            "  }",
            "",
            "  return {",
        ]
    )

    operations: List[str] = []
    used_names: Dict[str, int] = {}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, Mapping):
            continue
        common_parameters = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head"}:
                continue
            if not isinstance(operation, Mapping):
                continue
            parameters = list(common_parameters) + list(operation.get("parameters", []))
            name = _operation_name(method, path, operation)
            if name in used_names:
                used_names[name] += 1
                name = f"{name}{used_names[name]}"
            else:
                used_names[name] = 1
            path_parameters = [parameter for parameter in parameters if parameter.get("in") == "path"]
            query_parameters = [parameter for parameter in parameters if parameter.get("in") == "query"]
            body = operation.get("requestBody", {})
            body_schema = (
                body.get("content", {}).get("application/json", {}).get("schema", {})
                if isinstance(body, Mapping)
                else {}
            )
            has_body = bool(body_schema)
            has_params = bool(path_parameters or query_parameters)
            if has_params:
                required_names = [
                    parameter["name"]
                    for parameter in path_parameters + query_parameters
                    if parameter.get("required")
                ]
                parameter_fields = []
                for parameter in path_parameters + query_parameters:
                    optional = "" if parameter.get("required") else "?"
                    parameter_fields.append(
                        f"    {parameter['name']}{optional}: {_typescript_type(parameter.get('schema', {}))};"
                    )
                parameter_type = "{\n" + "\n".join(parameter_fields) + "\n  }"
                argument = f"params: {parameter_type}"
                if not required_names:
                    argument += " = {}"
            else:
                argument = ""
            if has_body:
                body_type = _typescript_type(body_schema)
                argument = f"{argument}, " if argument else ""
                argument += f"body: {body_type}"
            response_type = _typescript_type(_response_schema(operation))
            method_lines = [f"    {name}: async ({argument}) => {{"]
            method_lines.append(f"      const path = {_path_expression(path, parameters)};")
            if query_parameters:
                method_lines.append("      " + _query_expression(query_parameters).replace("\n", "\n      "))
            else:
                method_lines.append("      const requestPath = path;")
            request_options = [f'method: {json.dumps(method.upper())}']
            if has_body:
                request_options.append("body: JSON.stringify(body)")
            method_lines.append(
                f"      return request<{response_type}>(requestPath, {{ {', '.join(request_options)} }});"
            )
            method_lines.append("    },")
            operations.append("\n".join(method_lines))
    lines.extend("\n".join(operations).splitlines())
    type_name = _safe_identifier(client_name, "VedrakitClient")
    lines.extend(
        [
            "  };",
            "}",
            "",
            f"export type {type_name} = ReturnType<typeof createApiClient>;",
            "",
        ]
    )
    return "\n".join(lines)


def generate_typescript_client_file(
    document: Mapping[str, Any],
    output: str,
    *,
    client_name: str = "VedrakitClient",
) -> None:
    """Write a generated TypeScript client to ``output``."""
    from pathlib import Path

    Path(output).write_text(
        generate_typescript_client(document, client_name=client_name),
        encoding="utf-8",
    )