import { PUBLIC_SOCIAL_ACCOUNTS } from "./social-accounts";
import "./footer.css";

/** Shared logo links for Hail's public sections. Personal accounts stay excluded by the registry. */
export function SocialLinks() {
  return (
    <nav className="hail-footer-social" aria-label="Hail profiles">
      {PUBLIC_SOCIAL_ACCOUNTS.map(({ id, label, href }) => (
        <a key={id} href={href} aria-label={`Hail on ${label}`} title={label}>
          <span className={`hail-social-icon hail-social-${id}`} aria-hidden="true" />
        </a>
      ))}
    </nav>
  );
}
