/** Keep source Markdown relative. Resolve repository files only in rendered HTML. */
export function repositoryLink(
  href: string | undefined,
  pagePath: string,
): string | undefined {
  if (!href || !(href.startsWith("../") || href.startsWith("./"))) return href;
  const repository = new URL("https://github.com/hail-hq/hail/blob/main/");
  const document = new URL(`docs/public/${pagePath}`, repository);
  const target = new URL(href, document);
  // Published guide links stay with Fumadocs. Only other repository files go to GitHub.
  if (
    !target.pathname.startsWith(repository.pathname) ||
    target.pathname.startsWith(new URL("docs/public/", repository).pathname)
  )
    return href;
  return target.href;
}
