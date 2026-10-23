"""Resolve and validate operations from an observed OpenAPI contract."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnection, EndpointRequest
from clio_agent.gact.storage.ndp_streaming_models import PREFIX, sdk_operations


def connection_operations(connection: EndpointConnection) -> list[dict[str, Any]]:
    """Keep observed HTTP operations distinct from explicitly configured SDK operations."""
    observed = operations(connection.schema_document)
    if connection.streaming_runtime is not None:
        observed = [
            row
            for row in observed
            if row["path"] != "/streams" and not row["path"].startswith("/streams/")
        ]
    return [*observed, *sdk_operations(connection.streaming_runtime)]


def local_schema(value: Any, document: dict[str, Any], depth: int = 0) -> Any:
    """Expand local API references for forms/agents without fetching external documents."""
    if depth > 12:
        raise ValueError("Endpoint schema references are too deeply nested")
    if isinstance(value, list):
        return [local_schema(item, document, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/"):
            raise ValueError("Endpoint schemas must use local references")
        target: Any = document
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return local_schema(
            {**target, **{key: item for key, item in value.items() if key != "$ref"}},
            document,
            depth + 1,
        )
    return {key: local_schema(item, document, depth + 1) for key, item in value.items()}


def operations(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose explicit operation identities, parameters and bodies, never arbitrary routes."""
    rows = []
    if not isinstance(document.get("paths"), dict):
        raise ValueError("Endpoint OpenAPI must contain an object of operation paths")
    for path, item in document.get("paths", {}).items():
        if not isinstance(item, dict) or not isinstance(path, str) or not path.startswith("/"):
            continue
        if path in {"/user/login", "/token", "/oauth/token"}:
            continue  # Connection authentication uses CLIO's private account flow.
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"} or not isinstance(
                operation, dict
            ):
                continue
            # Auth headers/cookies are owned by CLIO, not supplied by the model.
            parameters = local_schema(
                [*item.get("parameters", []), *operation.get("parameters", [])], document
            )
            if path.startswith("/services/redirect/"):
                # NDP's proxy forwards arbitrary service query parameters; its
                # OpenAPI advertises the path but omits the upstream query schema.
                parameters.append(
                    {
                        "name": "query",
                        "in": "query-map",
                        "description": "Service query parameters as a JSON object",
                        "schema": {
                            "type": "object",
                            "additionalProperties": {"type": ["string", "number", "boolean"]},
                        },
                    }
                )
            rows.append(
                {
                    "id": f"{method.upper()} {path}",
                    "path": path,
                    "method": method.upper(),
                    "summary": operation.get("summary", operation.get("operationId", path)),
                    "description": str(operation.get("description", ""))[:4000],
                    "parameters": parameters,
                    "body_schema": local_schema(
                        operation.get("requestBody", {})
                        .get("content", {})
                        .get("application/json", {})
                        .get("schema"),
                        document,
                    ),
                    "multipart_schema": local_schema(
                        operation.get("requestBody", {})
                        .get("content", {})
                        .get("multipart/form-data", {})
                        .get("schema"),
                        document,
                    ),
                    "body_required": operation.get("requestBody", {}).get("required", False),
                    "read_only": method == "get",
                    "security": operation.get("security", document.get("security", [])),
                }
            )
    if len(rows) > 500:
        raise ValueError(
            "This Endpoint advertises too many operations; connect a narrower service API"
        )
    return rows


def validate_value(value: Any, schema: dict[str, Any], document: dict[str, Any]) -> None:
    """Validate locally; remote JSON Schema references are never fetched."""
    # Local $ref expansion does not cover every vocabulary (e.g. $dynamicRef).
    # An empty registry refuses any remaining reference without network retrieval.
    validator = Draft202012Validator(local_schema(schema, document), registry=Registry())
    try:
        errors = list(validator.iter_errors(value))
    except Unresolvable as exc:
        raise ValueError("Endpoint schema contains an unresolved external reference") from exc
    if errors:
        raise ValueError(
            "Endpoint parameter/body does not match its API: " + errors[0].message[:500]
        )


def resolve_request(
    connection: EndpointConnection, request: EndpointRequest, *, multipart: bool = False
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Validate a discovered route and return a same-origin, traversal-free URL."""
    operation = next(
        (row for row in connection_operations(connection) if row["id"] == request.operation_id),
        None,
    )
    if operation is None:
        raise ValueError("Refresh Endpoint operations and choose a returned operation ID")
    if request.operation_id.startswith(PREFIX):
        if request.body is not None:
            raise ValueError("SDK stream operations use named parameters, not an HTTP body")
        validate_value(
            request.parameters,
            {
                "type": "object",
                "properties": {row["name"]: row["schema"] for row in operation["parameters"]},
                "required": [row["name"] for row in operation["parameters"] if row["required"]],
                "additionalProperties": False,
            },
            {},
        )
        return operation, connection.url, request.parameters
    path, query = operation["path"], {}
    names = set()
    for parameter in operation["parameters"]:
        if "$ref" in parameter:
            raise ValueError(
                "This API uses referenced parameters; connect a service with inline parameters"
            )
        name, location = parameter.get("name"), parameter.get("in")
        names.add(name)
        if location == "query-map" and name in request.parameters:
            validate_value(
                request.parameters[name], parameter["schema"], connection.schema_document
            )
            query.update(request.parameters[name])
            continue
        if location not in {"query", "path", "query-map"}:
            if name in request.parameters or parameter.get("required"):
                raise ValueError(
                    "Endpoint headers and cookies must use private connection authentication"
                )
            continue
        if name not in request.parameters:
            if parameter.get("required"):
                raise ValueError(f"Endpoint parameter {name!r} is required")
            continue
        value = request.parameters[name]
        validate_value(value, parameter.get("schema", {}), connection.schema_document)
        if location == "path":
            text = str(value)
            if any(part in {".", ".."} for part in unquote(text).replace("\\", "/").split("/")):
                raise ValueError("Endpoint path parameters cannot traverse directories")
            path = path.replace("{" + name + "}", quote(text, safe=""))
        else:
            query[name] = str(value).lower() if isinstance(value, bool) else value
    if set(request.parameters) - names:
        raise ValueError("Only parameters advertised by this operation can be sent")
    if multipart and (operation["multipart_schema"] is None or request.body is not None):
        raise ValueError("Choose an advertised multipart upload without a JSON body")
    if request.body is None and operation["body_required"] and not multipart:
        if operation["multipart_schema"] is not None and operation["body_schema"] is None:
            raise ValueError("Use connected_data_ndp_upload_artifact for artifact file uploads")
        raise ValueError("This Endpoint operation requires a JSON body")
    if request.body is not None:
        if operation["body_schema"] is None:
            raise ValueError("This Endpoint operation does not advertise a JSON body")
        validate_value(request.body, operation["body_schema"], connection.schema_document)
        if len(json.dumps(request.body)) > 65536:
            raise ValueError("Keep an Endpoint request body below 64 KiB")
    if (
        "{" in path
        or "}" in path
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or any(part in {".", ".."} for part in unquote(path).replace("\\", "/").split("/"))
    ):
        raise ValueError("The Endpoint operation has an invalid path")
    return operation, connection.url + path, query
