import { loader } from "fumadocs-core/source";
import { docs } from "@/.source/server";

/**
 * `baseUrl` is "/", not "/docs" — Next's `basePath: "/docs"` already prefixes
 * every route and link, so setting it here too would emit /docs/docs/*.
 */
export const source = loader({
  baseUrl: "/",
  source: docs.toFumadocsSource(),
  // README.md is the index of its folder, matching how GitHub renders the same
  // directory. `setup/README.md` → /docs/setup, root `README.md` → /docs.
  slugs: (file) =>
    file.path
      .replace(/\.mdx?$/, "")
      .split("/")
      .filter((segment) => segment !== "README"),
});
