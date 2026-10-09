import { SITE_ORIGIN } from "@/lib/url";
import { SiteHeader } from "./hail-header/SiteHeader";
import { ThemeButton } from "./hail-footer/ThemeToggle";

export function Header() {
  return (
    <SiteHeader
      origin={SITE_ORIGIN}
      themeControl={<ThemeButton />}
    />
  );
}
