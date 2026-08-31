import { readFile } from "node:fs/promises";
import path from "node:path";
import { load as parseYaml } from "js-yaml";
import type { apiSource } from "@/lib/source";

type ApiPage = NonNullable<ReturnType<typeof apiSource.getPage>>;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type JSONSchema = Record<string, any>;

// The generated content/api/*.mdx files render via a client component that
// reads its `operations` prop at runtime — there's no typed page-data API for
// "which path+method is this page" outside that render path. The prop is a
// literal JSON array fumadocs-openapi always emits in this exact shape (see
// scripts/generate-openapi.mjs), so pull it back out of the raw file instead
// of re-deriving it. `per: "operation"` in that script means every generated
// page has exactly one entry.
function extractOperationRef(
  rawMdx: string,
): { path: string; method: string } | null {
  const match = rawMdx.match(/operations=\{(\[[^\]]*\])\}/);
  if (!match) return null;
  try {
    return JSON.parse(match[1])[0] ?? null;
  } catch {
    return null;
  }
}

let cachedDoc: Promise<JSONSchema> | undefined;
// Same file scripts/generate-openapi.mjs reads, with the same library — kept
// independent of fumadocs-openapi's internal document cache so this doesn't
// depend on how it keys/bundles schemas internally.
function loadOpenApiDoc(): Promise<JSONSchema> {
  cachedDoc ??= readFile(
    path.join(process.cwd(), "../openapi/openapi.yaml"),
    "utf8",
  ).then((text) => parseYaml(text) as JSONSchema);
  return cachedDoc;
}

interface FieldRow {
  field: string;
  type: string;
  required: boolean;
  description: string;
}

function resolveRef(doc: JSONSchema, ref: string): JSONSchema | undefined {
  const parts = ref.replace(/^#\//, "").split("/");
  let node: JSONSchema = doc;
  for (const part of parts) node = node?.[part];
  return node;
}

function resolveSchema(
  doc: JSONSchema,
  schema: JSONSchema | undefined,
): JSONSchema | undefined {
  if (!schema) return schema;
  if (schema.$ref) return resolveSchema(doc, resolveRef(doc, schema.$ref));
  return schema;
}

function typeLabel(doc: JSONSchema, schema: JSONSchema | undefined): string {
  const resolved = resolveSchema(doc, schema);
  if (!resolved) return "unknown";
  if (resolved.anyOf) {
    return resolved.anyOf.map((s: JSONSchema) => typeLabel(doc, s)).join(" | ");
  }
  if (resolved.enum) {
    return resolved.enum.map((v: unknown) => JSON.stringify(v)).join(" | ");
  }
  if (resolved.type === "array") return `${typeLabel(doc, resolved.items)}[]`;
  if (resolved.type === "object" || resolved.properties) return "object";
  if (resolved.format) return `${resolved.type} (${resolved.format})`;
  return resolved.type ?? "any";
}

function cell(value: string | undefined): string {
  return (value ?? "").replace(/\|/g, "\\|").replace(/\s+/g, " ").trim();
}

// Flattens a schema's properties into a field table, expanding one level of
// nested object/array-of-object properties (dotted paths) so a body like
// EmailCreate reads as one table instead of a maze of $refs. `seen` guards
// against a $ref cycle re-expanding itself forever.
function collectFields(
  doc: JSONSchema,
  schema: JSONSchema | undefined,
  prefix = "",
  depth = 0,
  seen: Set<string> = new Set(),
): FieldRow[] {
  const resolved = resolveSchema(doc, schema);
  if (!resolved) return [];
  if (resolved.anyOf) {
    const objectBranch = resolved.anyOf.find(
      (s: JSONSchema) => resolveSchema(doc, s)?.properties,
    );
    return objectBranch
      ? collectFields(doc, objectBranch, prefix, depth, seen)
      : [];
  }

  const properties: Record<string, JSONSchema> = resolved.properties ?? {};
  const required: string[] = resolved.required ?? [];
  const rows: FieldRow[] = [];

  for (const [name, propSchema] of Object.entries(properties)) {
    const resolvedProp = resolveSchema(doc, propSchema);
    const fieldPath = prefix ? `${prefix}.${name}` : name;
    rows.push({
      field: fieldPath,
      type: typeLabel(doc, propSchema),
      required: required.includes(name),
      description: resolvedProp?.description ?? propSchema.description ?? "",
    });

    if (depth >= 1 || !resolvedProp) continue;
    if (propSchema.$ref) {
      if (seen.has(propSchema.$ref)) continue;
      seen.add(propSchema.$ref);
    }
    const nested =
      (resolvedProp.properties && resolvedProp) ||
      (resolvedProp.type === "array" &&
        resolveSchema(doc, resolvedProp.items)?.properties &&
        resolveSchema(doc, resolvedProp.items));
    if (nested)
      rows.push(...collectFields(doc, nested, fieldPath, depth + 1, seen));
  }
  return rows;
}

function fieldsTable(rows: FieldRow[]): string {
  if (rows.length === 0) return "_None._";
  const header =
    "| Field | Type | Required | Description |\n| --- | --- | --- | --- |";
  const body = rows
    .map(
      (r) =>
        `| \`${r.field}\` | ${cell(r.type)} | ${r.required ? "yes" : "no"} | ${cell(r.description) || "—"} |`,
    )
    .join("\n");
  return `${header}\n${body}`;
}

function parametersTable(
  doc: JSONSchema,
  parameters: JSONSchema[] | undefined,
  location: "path" | "query" | "header",
): string | null {
  const filtered = (parameters ?? []).filter((p) => p.in === location);
  if (filtered.length === 0) return null;
  const header =
    "| Name | Type | Required | Description |\n| --- | --- | --- | --- |";
  const body = filtered
    .map((p) => {
      const description =
        p.description ?? resolveSchema(doc, p.schema)?.description ?? "";
      return `| \`${p.name}\` | ${cell(typeLabel(doc, p.schema))} | ${p.required ? "yes" : "no"} | ${cell(description) || "—"} |`;
    })
    .join("\n");
  return `${header}\n${body}`;
}

const LOCATION_TITLE = {
  path: "Path",
  query: "Query",
  header: "Header",
} as const;

function renderOperationMarkdown(
  doc: JSONSchema,
  path: string,
  method: string,
  operation: JSONSchema,
  title: string,
  description?: string,
): string {
  const sections: string[] = [
    `# ${title}`,
    `\`${method.toUpperCase()} ${path}\``,
  ];

  const opDescription = description ?? operation.description;
  if (opDescription) sections.push(opDescription);

  for (const location of ["path", "query", "header"] as const) {
    const table = parametersTable(doc, operation.parameters, location);
    if (table)
      sections.push(`## ${LOCATION_TITLE[location]} parameters\n\n${table}`);
  }

  const bodySchema =
    operation.requestBody?.content?.["application/json"]?.schema;
  if (bodySchema) {
    sections.push(
      `## Request body\n\n${fieldsTable(collectFields(doc, bodySchema))}`,
    );
  }

  const responses: Record<string, JSONSchema> = operation.responses ?? {};
  const codes = Object.keys(responses);
  if (codes.length) {
    const responseSections = codes.map((code) => {
      const response = responses[code];
      const schema = response.content?.["application/json"]?.schema;
      const parts = [`### ${code}`];
      if (response.description) parts.push(response.description);
      if (schema) parts.push(fieldsTable(collectFields(doc, schema)));
      return parts.join("\n\n");
    });
    sections.push(`## Responses\n\n${responseSections.join("\n\n")}`);
  }

  return `${sections.join("\n\n")}\n`;
}

/**
 * Renders one generated API-reference page's operation as plain markdown —
 * summary, parameters, and request/response schema fields with the same
 * descriptions the HTML page (`<OpenAPIPage />`) reads from openapi.yaml.
 */
export async function getOperationMarkdown(
  page: ApiPage,
): Promise<string | null> {
  const raw = await page.data.getText("raw");
  const op = extractOperationRef(raw);
  if (!op) return null;
  const doc = await loadOpenApiDoc();
  const operation = doc.paths?.[op.path]?.[op.method];
  if (!operation) return null;
  return renderOperationMarkdown(
    doc,
    op.path,
    op.method,
    operation,
    page.data.title,
    page.data.description,
  );
}

export function renderApiIndexMarkdown(pages: ApiPage[]): string {
  const lines = [
    "# API Reference",
    "Hail exposes a REST API at `https://api.hail.so`.",
    "Machine-readable spec: [openapi.yaml](/docs/api/openapi.yaml) · [openapi.json](/docs/api/openapi.json)",
    "## Operations",
    ...pages
      .slice()
      .sort((a, b) => a.data.title.localeCompare(b.data.title))
      .map((page) => `- [${page.data.title}](/docs${page.url}.md)`),
  ];
  return `${lines.join("\n\n")}\n`;
}
