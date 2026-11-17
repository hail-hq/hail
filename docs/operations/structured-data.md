# Costs and docs structured data

From the repository root, check the shared schema contracts:

```sh
pnpm run test:structured-data
pnpm run docs:build
pnpm run site:build
```

Use [`JsonLd`](../../shared/JsonLd.tsx) for every JSON-LD script. It escapes `<`
before rendering, including text derived from Markdown or model data.

| Page family                                      | Builder                                                  | Facts required                                                                                                                                  |
| ------------------------------------------------ | -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Docs guides, API index, generated API operations | [`articleSchema`](../../shared/structured-data.ts)       | Title, description, canonical path, existing docs image. Hail is the author and publisher. Both `Article` and `TechArticle` are explicit types. |
| Costs and comparison hubs                        | [`collectionSchema`](../../shared/structured-data.ts)    | Title, description, canonical path, visible items in order. Derive comparison items from `featuredPairs`.                                       |
| Featured model pairs                             | [`modelComparisonSchema`](../../web/lib/model-schema.ts) | Both checked model rows and their category. Names, provider, model IDs, and usage rates come from `costs/*.json`.                               |

These builders run in route templates. New published guides, generated API
operations, and featured model pairs inherit the schema automatically.
Keep both deployments' `ignoreCommand` watching `shared/`.

After adding a public guide, run `pnpm run docs:urls` and `pnpm run docs:check-urls`.
Update `DOCS` in `hail-website/lib/docs.ts` so the public docs links include it.

Docs do not yet carry trustworthy publication dates. Omit `datePublished` and
`dateModified`. Never substitute build time, the cost verification date, or the
latest commit for the original publication date. Add dates only with a reliable
per-document source, such as explicit editorial metadata or complete Git history.

Model offers describe provider usage rates. Each rate includes its currency and
billing unit. Use only the components visible in the table. A missing rate stays
missing. Deprecated models have no current offers. Do not invent a single
purchase price, availability, ratings, reviews, or product image. This markup
describes comparisons; it does not promise Google shopping eligibility.
The model creator is not always the rate vendor. Omit `seller` until the data
explicitly identifies the seller for each hosted rate.

An audit category is evidence. A setup guide is an article, even if an audit
calls it a product page. Match the rendered content rather than adding a fake
product to clear a warning.

To check every rendered sitemap page, start the built apps on unused ports:

```sh
pnpm --filter @hail-hq/docs-site exec next start --port 3422
pnpm --filter @hail-hq/web exec next start --port 3423
DOCS_TEST_ORIGIN=http://localhost:3422 COSTS_TEST_ORIGIN=http://localhost:3423 pnpm run test:structured-data
```

Run each server in its own terminal. The rendered test checks canonical URLs,
required fields, article types, collection lists, and both model products.
See [Next.js JSON-LD guidance](https://nextjs.org/docs/app/guides/json-ld),
[Schema.org TechArticle](https://schema.org/TechArticle), and
[unit pricing](https://schema.org/UnitPriceSpecification) for the contracts.
