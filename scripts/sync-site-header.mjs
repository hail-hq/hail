import { readFile, writeFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

// The costs header is the canonical cross-zone adaptation of hail.so's header.
const source = new URL("../web/components/hail-header/", import.meta.url);
const target = new URL("../docs-site/components/hail-header/", import.meta.url);
const check = process.argv.includes("--check");
if (!check) await mkdir(target, { recursive: true });
for (const name of [
  "SiteHeader.tsx",
  "SiteHeader.module.css",
  "BrandButton.module.css",
  "useDropdown.ts",
]) {
  const body = await readFile(new URL(name, source), "utf8");
  const path = new URL(name, target);
  if (check) {
    if ((await readFile(path, "utf8")) !== body)
      throw new Error(`Header out of sync: ${fileURLToPath(path)}`);
  } else await writeFile(path, body);
}
console.log(check ? "Site headers match." : "Synced site header to docs.");
