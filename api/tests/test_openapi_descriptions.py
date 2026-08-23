from hailhq.api.main import app


def test_every_public_operation_has_a_real_description() -> None:
    schema = app.openapi()
    missing = []
    for path, methods in schema["paths"].items():
        if path.startswith(("/internal", "/v1/internal")):
            continue
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            desc = (op.get("description") or "").strip()
            if len(desc) < 20:
                missing.append(f"{method.upper()} {path}")
    assert (
        not missing
    ), f"{len(missing)} operations still lack a real description: {missing}"


def _collect_schema_refs(node: object, found: set[str]) -> None:
    """Recursively collect every #/components/schemas/<Name> reference
    reachable from `node` — handles direct $ref, array `items`, and the
    `anyOf` shape Pydantic v2 uses for `X | None` fields."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.removeprefix("#/components/schemas/"))
        for key in ("items", "requestBody", "schema"):
            if key in node:
                _collect_schema_refs(node[key], found)
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in node.get(key, []):
                _collect_schema_refs(sub, found)
        content = node.get("content")
        if isinstance(content, dict):
            for media in content.values():
                _collect_schema_refs(media, found)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for prop_schema in properties.values():
                _collect_schema_refs(prop_schema, found)
    elif isinstance(node, list):
        for item in node:
            _collect_schema_refs(item, found)


def test_every_public_schema_field_has_a_description() -> None:
    schema = app.openapi()
    referenced_schemas: set[str] = set()
    for path, methods in schema["paths"].items():
        if path.startswith(("/internal", "/v1/internal")):
            continue
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if "requestBody" in op:
                _collect_schema_refs(op["requestBody"], referenced_schemas)
            for resp in op.get("responses", {}).values():
                _collect_schema_refs(resp, referenced_schemas)

    # Referenced schemas can themselves reference further schemas (e.g. a
    # response wraps a list of another model) — expand transitively until
    # the set stops growing.
    frontier = set(referenced_schemas)
    while frontier:
        new_refs: set[str] = set()
        for name in frontier:
            model = schema["components"]["schemas"].get(name, {})
            _collect_schema_refs(model, new_refs)
        frontier = new_refs - referenced_schemas
        referenced_schemas |= new_refs

    missing = []
    for name in sorted(referenced_schemas):
        model = schema["components"]["schemas"].get(name, {})
        for field_name, field_schema in model.get("properties", {}).items():
            if not field_schema.get("description"):
                missing.append(f"{name}.{field_name}")
    assert (
        not missing
    ), f"{len(missing)} public schema fields still lack a description: {missing}"
