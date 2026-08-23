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
