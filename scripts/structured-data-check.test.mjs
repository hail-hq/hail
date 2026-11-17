import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { matchesGlob } from "node:path";
import test from "node:test";
import { parse } from "yaml";

for (const [zone, missing] of [
  ["docs", "DOCS_TEST_ORIGIN"],
  ["costs", "COSTS_TEST_ORIGIN"],
]) {
  test(`required mode fails when ${missing} is missing`, () => {
    const env = { ...process.env, STRUCTURED_DATA_REQUIRED: "1" };
    delete env.DOCS_TEST_ORIGIN;
    delete env.COSTS_TEST_ORIGIN;
    // A nested Node test run must not inherit its parent's IPC test context.
    delete env.NODE_TEST_CONTEXT;
    const result = spawnSync(
      process.execPath,
      [
        "--test",
        "--test-reporter=tap",
        "--test-name-pattern",
        `^${zone} sitemap`,
        "scripts/structured-data.test.mjs",
      ],
      { env, encoding: "utf8", timeout: 10000 },
    );
    assert.equal(result.status, 1);
    assert.match(result.stdout, new RegExp(`${missing} is required`));
  });
}

test("the automatic check covers public-page inputs without waking for backend-only changes", () => {
  const workflow = parse(
    readFileSync(
      new URL("../.github/workflows/structured-data.yml", import.meta.url),
      "utf8",
    ),
  );
  const inputs = [
    "web/app/new/page.tsx",
    "docs-site/app/api/page.tsx",
    "shared/structured-data.ts",
    "docs/public/new-guide.md",
    "openapi/openapi.yaml",
    "costs/llm.json",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "vercel.json",
    "scripts/structured-data-check.mjs",
    "scripts/structured-data.test.mjs",
    "scripts/structured-data-check.test.mjs",
    ".github/workflows/structured-data.yml",
  ];
  for (const event of ["pull_request", "push"]) {
    const paths = workflow.on[event].paths;
    for (const input of inputs)
      assert.ok(
        paths.some((pattern) => matchesGlob(input, pattern)),
        `${event}: ${input}`,
      );
    for (const input of [
      "api/hailhq/api/main.py",
      "core/hailhq/core/models.py",
      "docs/operations/private.md",
    ]) {
      assert.ok(
        !paths.some((pattern) => matchesGlob(input, pattern)),
        `${event}: ${input}`,
      );
    }
  }
});
