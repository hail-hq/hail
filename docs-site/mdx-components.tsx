import defaultComponents from "fumadocs-ui/mdx";
import type { MDXComponents } from "mdx/types";
import { OpenAPIPage } from "@/components/openapi-page";

// The generated API-reference MDX (content/api/*) expects an `OpenAPIPage`
// component in its component map; it renders endpoint docs from the spec.
export function getMDXComponents(components?: MDXComponents): MDXComponents {
  return { ...defaultComponents, OpenAPIPage, ...components };
}
