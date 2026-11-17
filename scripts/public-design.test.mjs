import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const root = new URL("../", import.meta.url);
const read = (path) => readFileSync(new URL(path, root), "utf8");

for (const app of ["web", "docs-site"]) {
  test(`${app} loads the portable design after its framework CSS`, () => {
    const layout = read(`${app}/app/layout.tsx`);
    const styles = [
      'import "./global.css"',
      'import "@/components/hail-design/tokens.css"',
      'import "@/components/hail-design/typography.css"',
      'import "./public.css"',
    ];
    const positions = styles.map((style) => layout.indexOf(style));
    assert.ok(positions.every((position) => position >= 0));
    assert.deepEqual(
      positions,
      positions.toSorted((a, b) => a - b),
    );

    // Both zones must load the same font files locally. The portable tokens
    // need these variable names to resolve fonts instead of Arial fallbacks.
    assert.match(layout, /bricolage-grotesque-latin-wght-normal\.woff2/);
    for (const variable of [
      "--font-bricolage",
      "--font-geist-sans",
      "--font-geist-mono",
    ]) {
      assert.ok(layout.includes(`variable: "${variable}"`), variable);
    }
    assert.doesNotMatch(layout, /next\/font\/google/);
    const dependencies = JSON.parse(read(`${app}/package.json`)).dependencies;
    assert.ok(dependencies["@fontsource-variable/bricolage-grotesque"]);
  });
}

test("public app adapters use shared color roles instead of local palettes", () => {
  const docs = read("docs-site/app/public.css");
  const costs = read("web/app/public.css");
  assert.match(docs, /--color-fd-background:\s*var\(--paper\)/);
  assert.match(docs, /--color-fd-primary:\s*var\(--ac-text\)/);
  assert.match(costs, /--color-bg:\s*var\(--paper\)/);
  assert.match(costs, /--color-accent:\s*var\(--ac-text\)/);
  for (const css of [docs, costs]) {
    assert.doesNotMatch(css, /#[\da-f]{3,8}\b/i);
    assert.match(css, /var\(--hail-header-h, 0px\)/);
  }
});

test("public scope stays in the public layouts", () => {
  assert.match(
    read("docs-site/app/layout.tsx"),
    /className="hail-public hail-docs /,
  );
  assert.match(
    read("web/app/(dispatch)/layout.tsx"),
    /className="hail-public hail-costs"/,
  );
  assert.doesNotMatch(read("web/app/layout.tsx"), /className="hail-public/);
});

test("API operations render their own page title above the generated reference", () => {
  assert.match(
    read("docs-site/app/api/[[...slug]]/page.tsx"),
    /<DocsTitle>\{page\.data\.title\}<\/DocsTitle>/,
  );
});

test("docs search uses its own base path and both public content sources", () => {
  assert.match(
    read("docs-site/app/layout.tsx"),
    /search=\{\{ options: \{ api: "\/docs\/api\/search" \} \}\}/,
  );
  const route = read("docs-site/app/api/search/route.ts");
  assert.match(route, /source\.getPages\(\), \.\.\.apiSource\.getPages\(\)/);
});

test(
  "docs search returns guides, API operations, and an empty result for unknown words",
  { skip: !process.env.DOCS_TEST_ORIGIN },
  async () => {
    for (const [query, expected] of [
      ["Agents", "/agents"],
      ["Create Call", "/api/create_call_v1_calls_post"],
      ["zzqvpublicdesignnosuchword", null],
    ]) {
      const url = new URL("/docs/api/search", process.env.DOCS_TEST_ORIGIN);
      url.searchParams.set("query", query);
      const response = await fetch(url);
      assert.equal(response.status, 200, query);
      const results = await response.json();
      if (expected) {
        assert.ok(
          results.some((result) => result.url === expected),
          query,
        );
      } else {
        assert.deepEqual(results, []);
      }
    }
  },
);

for (const [zone, origin, paths] of [
  [
    "docs",
    process.env.DOCS_TEST_ORIGIN,
    ["/docs", "/docs/api/create_call_v1_calls_post"],
  ],
  ["costs", process.env.COSTS_TEST_ORIGIN, ["/costs", "/costs/compare"]],
]) {
  test(
    `${zone} public pages render their shared scope and local display font`,
    { skip: !origin },
    async () => {
      for (const path of paths) {
        const response = await fetch(`${origin}${path}`);
        assert.equal(response.status, 200, path);
        const html = await response.text();
        assert.match(html, /class="hail-public hail-(docs|costs)/);
        assert.equal(
          [...html.matchAll(/<h1(?:\s|>)/g)].length,
          1,
          `${path} has one page title`,
        );
        const styles = [
          ...html.matchAll(/<link\b[^>]*href="([^"]+\.css[^\"]*)"/g),
        ];
        assert.ok(styles.length > 0, `${path} has CSS`);
        const css = (
          await Promise.all(
            styles.map(async ([, href]) => {
              const file = await fetch(
                new URL(href.replaceAll("&amp;", "&"), origin),
              );
              assert.equal(file.status, 200, href);
              return file.text();
            }),
          )
        ).join("\n");
        assert.match(css, /--public-paper/);
        assert.match(css, /--font-bricolage/);
        assert.match(css, /\.hail-public/);
      }
    },
  );
}
