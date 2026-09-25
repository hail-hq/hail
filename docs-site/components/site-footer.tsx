import { Footer } from "./hail-footer/Footer";
import groups from "./hail-footer/navigation.json";
import { FooterTheme } from "./footer-theme";

export function SiteFooter() {
  return (
    <Footer
      homeHref="https://hail.so"
      groups={groups.map((group) => ({
        ...group,
        links: group.links.map((link) => ({
          ...link,
          href: new URL(link.href, "https://hail.so").href,
        })),
      }))}
      themeControl={<FooterTheme />}
    />
  );
}
