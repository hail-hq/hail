import { PUBLIC_SOCIAL_ACCOUNTS } from "./social-accounts";
import "./footer.css";

/** Shared logo links for Hail's public sections. Personal accounts stay excluded by the registry. */
export function SocialLinks({ navLabel = "Hail profiles", profileLabel = (network: string) => `Hail on ${network}` }: { navLabel?: string; profileLabel?: (network: string) => string } = {}) {
  return (
    <nav className="hail-footer-social" aria-label={navLabel}>
      {PUBLIC_SOCIAL_ACCOUNTS.map(({ id, label, href }) => (
        <a key={id} href={href} aria-label={profileLabel(label)} title={label}>
          <span className={`hail-social-icon hail-social-${id}`} aria-hidden="true" />
        </a>
      ))}
    </nav>
  );
}
