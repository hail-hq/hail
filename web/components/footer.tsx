import { siteHref } from "@/lib/url";
import { Footer as SharedFooter } from "./hail-footer/Footer";
import groups from "./hail-footer/navigation.json";
import badges from "./hail-footer/badges.json";
import { ThemeToggle } from "./hail-footer/ThemeToggle";

export function Footer() {
  return (
    <SharedFooter
      badges={badges}
      homeHref={siteHref("/")}
      groups={groups.map((group) => ({
        ...group,
        links: group.links.map((link) => ({
          ...link,
          href: siteHref(link.href),
        })),
      }))}
      themeControl={<ThemeToggle />}
    />
  );
}
