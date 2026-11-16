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
    <div className="costs-notice" role="note">
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
        <div className="costs-notice-link">
          <a href={`/costs/compare/${successorSlug}`}>
            See the current comparison →
          </a>
        </div>
      ) : null}
    </div>
  );
}
