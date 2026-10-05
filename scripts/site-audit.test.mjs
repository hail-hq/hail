import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import test from "node:test";
import { docsPath, pageMetadata } from "../shared/site-metadata.ts";

const root = new URL("../", import.meta.url);

test("each docs path has its own canonical and complete social metadata", () => {
  const paths = JSON.parse(
    readFileSync(new URL("docs-site/urls.json", root), "utf8"),
  );
  paths.push("/docs/api", "/docs/api/create_call_v1_calls_post");
  for (const path of paths) {
    const loaderPath = path === "/docs" ? "/" : path.slice(5);
    assert.equal(docsPath(loaderPath), path);
    const metadata = pageMetadata({
      title: "Guide | hail.so",
      description: "Guide",
      path,
      image: "/docs/opengraph-image",
    });
    assert.equal(metadata.alternates.canonical, `https://hail.so${path}`);
    assert.equal(metadata.openGraph.url, metadata.alternates.canonical);
    assert.deepEqual(metadata.twitter.images, metadata.openGraph.images);
  }
});

test("repository links in audited guides point to existing GitHub files", () => {
  for (const path of [
    "docs/public/cli.md",
    "docs/public/self-host/telnyx.md",
    "docs/public/self-host/didww.md",
  ]) {
    const markdown = readFileSync(new URL(path, root), "utf8");
    assert.doesNotMatch(
      markdown,
      /\]\(\.\.\/(?:\.\.\/)*(?:api|core|openapi)\//,
    );
    for (const [, target] of markdown.matchAll(
      /https:\/\/github\.com\/hail-hq\/hail\/blob\/main\/([^\s)]+)/g,
    )) {
      assert.ok(existsSync(new URL(target, root)), `${path}: ${target}`);
    }
  }
});

for (const [zone, origin] of [
  ["docs", process.env.DOCS_TEST_ORIGIN],
  ["costs", process.env.COSTS_TEST_ORIGIN],
]) {
  test(
    `${zone} sitemap pages render self canonicals and social URLs`,
    { skip: !origin },
    async () => {
      const response = await fetch(`${origin}/${zone}/sitemap.xml`);
      assert.equal(response.status, 200);
      const sitemap = await response.text();
      const urls = [...sitemap.matchAll(/<loc>(.*?)<\/loc>/g)].map(([, url]) =>
        url.replaceAll("&amp;", "&"),
      );
      assert.ok(urls.length > 1);
      assert.equal(new Set(urls).size, urls.length);
      for (let start = 0; start < urls.length; start += 6) {
        await Promise.all(
          urls.slice(start, start + 6).map(async (url) => {
            const path = new URL(url).pathname;
            const page = await fetch(`${origin}${path}`);
            assert.equal(page.status, 200, url);
            const html = await page.text();
            const title = html.match(/<title>(.*?)<\/title>/)?.[1];
            assert.ok(title && title.length <= 60, `${url}: title length`);
            const canonical = html.match(
              /<link[^>]*rel="canonical"[^>]*href="([^"]+)"/,
            );
            assert.equal(canonical?.[1], url, `${url}: canonical`);
            const ogUrl = html.match(
              /<meta[^>]*property="og:url"[^>]*content="([^"]+)"/,
            );
            assert.equal(ogUrl?.[1], url, `${url}: og:url`);
            assert.ok(html.includes('property="og:image"'), `${url}: og:image`);
            assert.ok(
              html.includes('name="twitter:image"'),
              `${url}: twitter:image`,
            );
            assert.ok(
              !html.includes('href="/wallet"') &&
                !html.includes('href="https://hail.so/wallet"'),
              `${url}: broken wallet link`,
            );
          }),
        );
      }
    },
  );
}
