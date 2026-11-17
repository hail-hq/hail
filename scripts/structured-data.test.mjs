import assert from "node:assert/strict";
import test from "node:test";
import {
  readFileSync,
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  rmSync,
} from "node:fs";
import { execFileSync, spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import {
  articleSchema,
  collectionSchema,
  serializeJsonLd,
} from "../shared/structured-data.ts";
import {
  modelComparisonSchema,
  modelProduct,
} from "../web/lib/model-schema.ts";

function assertBuildFilters(configFiles, changedPath) {
  const directory = mkdtempSync(join(tmpdir(), "hail-structured-data-deploy-"));
  try {
    const git = (...args) =>
      execFileSync("git", args, { cwd: directory, encoding: "utf8" });
    git("init", "--quiet", "--initial-branch=fixture");
    git("config", "user.name", "Test");
    git("config", "user.email", "test@example.com");
    mkdirSync(join(directory, dirname(changedPath)), { recursive: true });
    mkdirSync(join(directory, "app"));
    writeFileSync(join(directory, changedPath), "before");
    git("add", changedPath);
    git("commit", "--quiet", "-m", "Initial fixture");
    const base = git("rev-parse", "HEAD").trim();
    writeFileSync(join(directory, changedPath), "after");
    git("add", changedPath);
    git("commit", "--quiet", "-m", "Change build input");
    for (const file of configFiles) {
      const { ignoreCommand } = JSON.parse(
        readFileSync(new URL(file, import.meta.url)),
      );
      for (const cwd of [directory, join(directory, "app")]) {
        const run = spawnSync("sh", ["-c", ignoreCommand], {
          cwd,
          env: { ...process.env, VERCEL_GIT_PREVIOUS_SHA: base },
        });
        assert.equal(
          run.status,
          1,
          `${file}: ${changedPath} must request a build from ${cwd}`,
        );
      }
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("both apps rebuild when only shared structured data changes", () => {
  assertBuildFilters(
    ["../vercel.json", "../docs-site/vercel.json"],
    "shared/schema.ts",
  );
});

test("docs rebuild generated API pages when only the OpenAPI spec changes", () => {
  assertBuildFilters(["../docs-site/vercel.json"], "openapi/openapi.yaml");
});

test("Vercel build filters fit its 256 character limit", () => {
  for (const file of ["../vercel.json", "../docs-site/vercel.json"]) {
    const { ignoreCommand } = JSON.parse(
      readFileSync(new URL(file, import.meta.url)),
    );
    assert.ok(
      ignoreCommand.length <= 256,
      `${file}: ${ignoreCommand.length} characters`,
    );
  }
});

test("a hosted model rate does not label the model creator as the seller", () => {
  const { models } = JSON.parse(
    readFileSync(new URL("../costs/llm.json", import.meta.url)),
  );
  const model = models.find((row) => row.model_id === "llama-4-maverick");
  const product = modelProduct(
    model,
    "llm",
    "https://hail.so/costs/compare/example",
  );
  assert.equal(product.brand.name, "Meta");
  assert.ok(product.offers.length > 0);
  for (const offer of product.offers) {
    assert.match(offer.url, /amazonaws\.com/);
    assert.ok(!offer.seller);
  }
});

test("article schema identifies the canonical document and its real publisher", () => {
  const article = articleSchema({
    title: "Webhooks",
    description: "Receive events from Hail.",
    path: "/docs/webhooks",
    image: "/docs/opengraph-image",
  });
  assert.deepEqual(article["@type"], ["Article", "TechArticle"]);
  assert.equal(article.headline, "Webhooks");
  assert.equal(article.url, "https://hail.so/docs/webhooks");
  assert.equal(article.mainEntityOfPage["@id"], article.url);
  assert.equal(article.image, "https://hail.so/docs/opengraph-image");
  assert.equal(article.author.name, "Hail");
  assert.equal(article.publisher["@id"], "https://hail.so/#organization");
  assert.ok(!("datePublished" in article) && !("dateModified" in article));
  assert.throws(() =>
    articleSchema({
      title: "",
      description: "Guide",
      path: "/docs",
      image: "/docs/opengraph-image",
    }),
  );
});

test("collections keep visible item order and resolve public URLs once", () => {
  const result = collectionSchema({
    title: "Costs",
    description: "Compare model costs.",
    path: "/costs",
    origin: "https://hail.so/",
    items: [
      { name: "Language models", path: "/costs#llm" },
      { name: "Speech", path: "/costs#stt" },
    ],
  });
  const [page, list] = result["@graph"];
  assert.equal(page.mainEntity["@id"], "https://hail.so/costs#list");
  assert.equal(list.numberOfItems, 2);
  assert.deepEqual(
    list.itemListElement.map(({ position, url }) => [position, url]),
    [
      [1, "https://hail.so/costs#llm"],
      [2, "https://hail.so/costs#stt"],
    ],
  );
  assert.throws(() =>
    collectionSchema({
      title: "Costs",
      description: "Costs",
      path: "/costs",
      items: [],
    }),
  );
});

test("JSON-LD cannot close its script element with untrusted content", () => {
  const input = { headline: "</script><script>alert('x')</script>" };
  const serialized = serializeJsonLd(input);
  assert.ok(!serialized.includes("<"));
  assert.deepEqual(JSON.parse(serialized), input);
});

test("model offers preserve currency and billing units without inventing a purchase price", () => {
  const model = {
    display_name: "Example speech",
    model_id: "speech-1",
    provider: "Example",
    source_url: "https://example.com/pricing",
    price_per_minute_eur: "0.006",
  };
  const product = modelProduct(
    model,
    "stt",
    "https://hail.so/costs/compare/example",
  );
  assert.equal(product.model, "speech-1");
  assert.equal(product.offers[0].url, "https://example.com/pricing");
  assert.deepEqual(product.offers[0].priceSpecification, {
    "@type": "UnitPriceSpecification",
    price: "0.006",
    priceCurrency: "EUR",
    unitText: "1 minute of audio",
  });
  assert.ok(
    !("price" in product.offers[0]) && !("availability" in product.offers[0]),
  );
  for (const price of [undefined, "unknown", "-1", "Infinity"]) {
    assert.ok(
      !modelProduct(
        { ...model, price_per_minute_eur: price },
        "stt",
        "https://hail.so/costs",
      ).offers,
    );
  }
  assert.ok(
    !modelProduct(
      { ...model, deprecated_at: "2026-01-01" },
      "stt",
      "https://hail.so/costs",
    ).offers,
  );
});

test("every featured model gets a factual Product when new comparisons are generated", () => {
  let count = 0;
  for (const category of ["llm", "stt", "tts"]) {
    const { models } = JSON.parse(
      readFileSync(new URL(`../costs/${category}.json`, import.meta.url)),
    );
    const featured = models.filter((model) => model.featured);
    for (let i = 0; i < featured.length; i++) {
      for (let j = i + 1; j < featured.length; j++) {
        const result = modelComparisonSchema({
          models: [featured[i], featured[j]],
          category,
          path: "/costs/compare/example",
          origin: "https://hail.so",
        });
        const [page, a, b] = result["@graph"];
        assert.deepEqual(
          [a.name, b.name],
          [featured[i].display_name, featured[j].display_name],
        );
        assert.deepEqual(page.mainEntity, [
          { "@id": a["@id"] },
          { "@id": b["@id"] },
        ]);
        for (const product of [a, b]) {
          assert.equal(product["@type"], "Product");
          assert.ok(product.brand.name && product.description && product.model);
          assert.ok(
            !product.image && !product.aggregateRating && !product.review,
          );
          for (const offer of product.offers ?? []) {
            assert.ok(!offer.availability && !offer.priceValidUntil);
            assert.ok(offer.priceSpecification.unitText);
          }
        }
        count++;
      }
    }
  }
  assert.ok(count >= 17);
});

function nodesFrom(html) {
  return [
    ...html.matchAll(
      /<script[^>]*type="application\/ld\+json"[^>]*>(.*?)<\/script>/gs,
    ),
  ].flatMap(([, body]) => {
    const value = JSON.parse(body);
    return value["@graph"] ?? [value];
  });
}

for (const [zone, origin] of [
  ["docs", process.env.DOCS_TEST_ORIGIN],
  ["costs", process.env.COSTS_TEST_ORIGIN],
]) {
  test(
    `${zone} sitemap pages include the correct structured data`,
    { skip: !origin },
    async () => {
      const sitemap = await fetch(`${origin}/${zone}/sitemap.xml`).then((r) =>
        r.text(),
      );
      const urls = [...sitemap.matchAll(/<loc>(.*?)<\/loc>/g)].map(([, url]) =>
        url.replaceAll("&amp;", "&"),
      );
      assert.ok(urls.length > 1);
      for (const url of urls) {
        const path = new URL(url).pathname;
        const response = await fetch(`${origin}${path}`);
        assert.equal(response.status, 200, path);
        const nodes = nodesFrom(await response.text());
        const hasType = (node, type) => [node["@type"]].flat().includes(type);
        if (zone === "docs") {
          const article = nodes.find((node) => hasType(node, "Article"));
          assert.ok(article, `${path}: Article`);
          assert.ok(hasType(article, "TechArticle"), `${path}: TechArticle`);
          for (const field of [
            "headline",
            "description",
            "image",
            "author",
            "publisher",
            "mainEntityOfPage",
          ]) {
            assert.ok(article[field], `${path}: ${field}`);
          }
          assert.equal(article.url, url, `${path}: canonical URL`);
        } else if (path.includes("/compare/")) {
          const products = nodes.filter((node) => hasType(node, "Product"));
          assert.equal(products.length, 2, `${path}: two model products`);
          for (const product of products) {
            assert.ok(
              product.name && product.model && product.brand && product.url,
            );
            assert.ok(
              !product.image && !product.aggregateRating && !product.review,
            );
          }
        } else {
          const list = nodes.find((node) => hasType(node, "ItemList"));
          assert.ok(list?.itemListElement.length > 0, `${path}: ItemList`);
          assert.ok(
            nodes.some((node) => hasType(node, "CollectionPage")),
            `${path}: CollectionPage`,
          );
        }
      }
    },
  );
}
