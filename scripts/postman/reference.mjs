// Hail's public OpenAPI file uses local schema refs. Keep this exporter small:
// no schema faker, remote imports, live calls, or external schema resolution.
export const httpMethods = new Set([
  "get",
  "post",
  "put",
  "patch",
  "delete",
  "head",
  "options",
]);

export function resolve(spec, schema) {
  if (!schema?.$ref) return schema ?? {};
  if (!schema.$ref.startsWith("#/"))
    throw new Error("Only local OpenAPI references are supported");
  return schema.$ref
    .slice(2)
    .split("/")
    .reduce((value, key) => value[key], spec);
}

export function example(spec, input, depth = 0) {
  const schema = resolve(spec, input);
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (schema.enum) return schema.enum[0];
  if (schema.const !== undefined) return schema.const;
  if (depth > 8) return null;
  if (schema.anyOf || schema.oneOf)
    return example(
      spec,
      (schema.anyOf ?? schema.oneOf).find((s) => s.type !== "null") ?? {},
      depth + 1,
    );
  if (schema.allOf)
    return Object.assign(
      {},
      ...schema.allOf.map((s) => example(spec, s, depth + 1)),
    );
  const type = Array.isArray(schema.type)
    ? schema.type.find((t) => t !== "null")
    : schema.type;
  if (type === "object" || schema.properties)
    return Object.fromEntries(
      Object.entries(schema.properties ?? {}).map(([key, value]) => [
        key,
        example(spec, value, depth + 1),
      ]),
    );
  if (type === "array") return [example(spec, schema.items ?? {}, depth + 1)];
  if (type === "boolean") return false;
  if (type === "integer" || type === "number") return schema.minimum ?? 0;
  if (type === "null") return null;
  return (
    {
      uuid: "00000000-0000-4000-a000-000000000000",
      email: "person@example.com",
      "date-time": "2026-01-01T00:00:00Z",
      date: "2026-01-01",
      uri: "https://example.com",
    }[schema.format] ?? "string"
  );
}

function fieldNotes(spec, schema) {
  const body = resolve(spec, schema);
  if (!body.properties) return "";
  const rows = Object.entries(body.properties).map(([key, value]) => {
    const field = resolve(spec, value);
    const type =
      field.type ??
      (field.anyOf
        ? field.anyOf
            .map((s) => s.type ?? s.$ref?.split("/").at(-1))
            .join(" / ")
        : "object");
    const description = (field.description ?? "")
      .replaceAll("|", "\\|")
      .replaceAll("\n", " ");
    return `| ${key} | ${type} | ${body.required?.includes(key) ? "yes" : "no"} | ${description} |`;
  });
  return `\n\n### Body fields\n\n| Field | Type | Required | Description |\n| --- | --- | --- | --- |\n${rows.join("\n")}`;
}

export function referenceItems(spec) {
  const folders = new Map();
  for (const [path, definition] of Object.entries(spec.paths)) {
    for (const [method, operation] of Object.entries(definition)) {
      if (!httpMethods.has(method)) continue;
      const params = [
        ...(definition.parameters ?? []),
        ...(operation.parameters ?? []),
      ].map((p) => resolve(spec, p));
      const query = params
        .filter((p) => p.in === "query")
        .map((p) => ({
          key: p.name,
          value: String(example(spec, p.schema)),
          description: p.description ?? "",
          disabled: !p.required,
        }));
      // Hail declares Authorization as a header parameter. Routes without it
      // are public; the rest use the collection's bearer auth.
      const isAuth = (p) =>
        p.in === "header" && p.name.toLowerCase() === "authorization";
      const urlPath = path.replace(/\{([^}]+)\}/g, ":$1");
      const enabledQuery = query
        .filter((p) => !p.disabled)
        .map(
          (p) => `${encodeURIComponent(p.key)}=${encodeURIComponent(p.value)}`,
        )
        .join("&");
      const request = {
        method: method.toUpperCase(),
        header: params
          .filter((p) => p.in === "header" && !isAuth(p))
          .map((p) => ({
            key: p.name,
            value: String(example(spec, p.schema)),
            disabled: !p.required,
          })),
        url: {
          raw: `{{baseUrl}}${urlPath}${enabledQuery ? `?${enabledQuery}` : ""}`,
          host: ["{{baseUrl}}"],
          path: urlPath.split("/").filter(Boolean),
          query,
          variable: params
            .filter((p) => p.in === "path")
            .map((p) => ({
              key: p.name,
              value: String(example(spec, p.schema)),
            })),
        },
        description: operation.description ?? operation.summary ?? "",
      };
      if (!params.some(isAuth)) request.auth = { type: "noauth" };
      const content = resolve(spec, operation.requestBody).content;
      if (content?.["application/json"]?.schema) {
        const schema = content["application/json"].schema;
        request.body = {
          mode: "raw",
          raw: JSON.stringify(example(spec, schema), null, 2),
          options: { raw: { language: "json" } },
        };
        request.header.push({ key: "Content-Type", value: "application/json" });
        request.description += fieldNotes(spec, schema);
      } else if (content?.["multipart/form-data"]?.schema) {
        const schema = resolve(spec, content["multipart/form-data"].schema);
        request.body = {
          mode: "formdata",
          formdata: Object.entries(schema.properties ?? {}).map(
            ([key, value]) => ({
              key,
              type: value.format === "binary" ? "file" : "text",
              ...(value.format === "binary"
                ? { src: [] }
                : { value: String(example(spec, value)) }),
            }),
          ),
        };
      }
      const responses = Object.entries(operation.responses ?? {})
        .filter(([code]) => /^\d{3}$/.test(code))
        .map(([code, input]) => {
          const response = resolve(spec, input);
          const schema = response.content?.["application/json"]?.schema;
          return {
            name: `${code}: ${response.description ?? "Schema example"}`,
            code: Number(code),
            status: response.description ?? "Schema example",
            header: schema
              ? [{ key: "Content-Type", value: "application/json" }]
              : [],
            body: schema ? JSON.stringify(example(spec, schema), null, 2) : "",
            originalRequest: structuredClone(request),
            _postman_previewlanguage: "json",
          };
        });
      const tag = operation.tags?.[0] ?? "Other";
      if (!folders.has(tag)) folders.set(tag, { name: tag, item: [] });
      folders.get(tag).item.push({
        name:
          operation.summary ??
          operation.operationId ??
          `${method.toUpperCase()} ${path}`,
        request,
        response: responses,
      });
    }
  }
  return [...folders.values()];
}
