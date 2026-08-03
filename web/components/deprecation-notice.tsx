import type { FeaturedRow } from "@/lib/featured";

export function DeprecationNotice({
  models,
  successorSlug,
}: {
  models: FeaturedRow[];
  successorSlug: string | null;
}) {
  const deprecated = models.filter((m) => m.deprecated_at);
  if (deprecated.length === 0) return null;

  return (
    <div
      style={{
        border: "2px solid var(--color-ink)",
        background: "var(--color-paper)",
        padding: "14px 18px",
        marginBottom: 24,
        fontFamily: "var(--font-mono)",
        fontSize: 12,
      }}
      role="note"
    >
      {deprecated.map((m) => (
        <div key={m.model_id}>
          <b>{m.display_name}</b> was deprecated on {m.deprecated_at}.
          {m.replaced_by_model_id ? (
            <>
              {" "}
              Replaced by <code>{m.replaced_by_model_id}</code>.
            </>
          ) : null}
        </div>
      ))}
      {successorSlug ? (
        <div style={{ marginTop: 8 }}>
          <a href={`/costs/compare/${successorSlug}`}>
            See the current comparison →
          </a>
        </div>
      ) : null}
    </div>
  );
}
