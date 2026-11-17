import { serializeJsonLd, type JsonLdData } from "./structured-data";

/** Server-rendered JSON-LD. Keep all script serialization in this component. */
export function JsonLd({ data }: { data: JsonLdData }) {
  return (
    <script
      type="application/ld+json"
      dangerouslySetInnerHTML={{ __html: serializeJsonLd(data) }}
    />
  );
}
