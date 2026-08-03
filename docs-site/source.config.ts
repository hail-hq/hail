import { metaSchema, pageSchema } from "fumadocs-core/source/schema";
import { defineConfig, defineDocs } from "fumadocs-mdx/config";
import { z } from "zod";
import { deriveDescription, deriveTitle } from "./lib/derive-meta";

/**
 * The published docs corpus is `docs/public/` in this repo — the folder IS the
 * allowlist. Anything inside it renders at hail.so/docs; anything outside it
 * (superpowers plans, submissions, legal, runbooks) never ships. Don't add a
 * denylist here; move the file instead.
 */
export const docs = defineDocs({
  dir: "../docs/public",
  docs: {
    // Schema is a function so it can read the raw file: these docs have no
    // frontmatter, so title/description are derived from the H1 and lead
    // paragraph. An explicit frontmatter key still overrides the default.
    schema: ({ source }) => {
      const description = deriveDescription(source);
      return pageSchema.extend({
        title: z.string().default(deriveTitle(source)),
        // Only supply a default when one could actually be derived — zod's
        // `.default()` rejects undefined, and a doc with no lead paragraph
        // should end up with no description rather than an empty string.
        description: description
          ? z.string().default(description)
          : z.string().optional(),
      });
    },
  },
  meta: { schema: metaSchema },
});

/**
 * API reference pages, generated from openapi/openapi.yaml into content/api by
 * scripts/generate-openapi.mjs (run before every build). Generated files carry
 * their own frontmatter, so no derivation is needed here.
 */
export const apiDocs = defineDocs({
  dir: "content/api",
});

export default defineConfig();
