/** Shared schema contracts for the independently deployed costs and docs apps. */
export type JsonLdData = Record<string, unknown>;

export function serializeJsonLd(data: JsonLdData): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}

type PageDetails = {
  title: string;
  description: string;
  path: string;
  origin?: string;
};

function pageDetails({
  title,
  description,
  path,
  origin = process.env.NEXT_PUBLIC_SITE_ORIGIN || "https://hail.so",
}: PageDetails) {
  for (const [field, value] of Object.entries({ title, description, path })) {
    if (!value.trim()) throw new Error(`Structured data needs ${field}.`);
  }
  return { title, description, url: new URL(path, origin).href, origin };
}

export function articleSchema(input: PageDetails & { image: string }) {
  const { title, description, url, origin } = pageDetails(input);
  if (!input.image.trim()) throw new Error("Structured data needs image.");
  const organization = {
    "@type": "Organization",
    "@id": new URL("/#organization", origin).href,
    name: "Hail",
    url: new URL("/", origin).href,
  };
  return {
    "@context": "https://schema.org",
    // Name the parent explicitly: some audit parsers do not expand subtypes.
    "@type": ["Article", "TechArticle"],
    "@id": `${url}#article`,
    url,
    headline: title,
    description,
    image: new URL(input.image, origin).href,
    author: organization,
    publisher: organization,
    mainEntityOfPage: { "@type": "WebPage", "@id": url },
    inLanguage: "en",
  };
}

export function collectionSchema(
  input: PageDetails & {
    items: readonly { name: string; path: string }[];
  },
) {
  const { title, description, url, origin } = pageDetails(input);
  if (!input.items.length) throw new Error("A collection needs visible items.");
  const list = {
    "@type": "ItemList",
    "@id": `${url}#list`,
    numberOfItems: input.items.length,
    itemListElement: input.items.map((item, index) => {
      if (!item.name.trim() || !item.path.trim()) {
        throw new Error("A collection item needs a name and path.");
      }
      return {
        "@type": "ListItem",
        position: index + 1,
        name: item.name,
        url: new URL(item.path, origin).href,
      };
    }),
  };
  return {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "CollectionPage",
        "@id": url,
        url,
        name: title,
        description,
        mainEntity: { "@id": list["@id"] },
      },
      list,
    ],
  };
}
