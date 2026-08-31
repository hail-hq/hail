import { getDocsLLMText } from "@/lib/get-llm-text";
import { getOperationMarkdown } from "@/lib/openapi-markdown";
import { apiSource, source } from "@/lib/source";

export const revalidate = false;

export async function GET() {
  const guides = await Promise.all(source.getPages().map(getDocsLLMText));
  const operations = (
    await Promise.all(apiSource.getPages().map(getOperationMarkdown))
  ).filter((text): text is string => text !== null);

  return new Response([...guides, ...operations].join("\n\n---\n\n"), {
    headers: { "content-type": "text/markdown; charset=utf-8" },
  });
}
