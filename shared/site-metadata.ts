/** Complete page metadata for the independently deployed docs and costs apps. */
export function pageMetadata({
  title,
  description,
  path,
  image,
  origin = "https://hail.so",
}: {
  title: string;
  description: string;
  path: string;
  image: string;
  origin?: string;
}) {
  const url = new URL(path, origin).href;
  const images = [{ url: image, width: 1200, height: 630, alt: title }];
  return {
    title: { absolute: title },
    description,
    alternates: { canonical: url },
    openGraph: {
      type: "website" as const,
      url,
      siteName: "Hail",
      title,
      description,
      images,
    },
    twitter: {
      card: "summary_large_image" as const,
      site: "@hail_hq",
      title,
      description,
      images,
    },
  };
}

/** Loader URLs omit Next's basePath. Add it exactly once for public metadata. */
export function docsPath(loaderPath: string): string {
  return loaderPath === "/" ? "/docs" : `/docs${loaderPath}`;
}
