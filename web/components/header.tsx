import { SITE_ORIGIN } from "@/lib/url";
import { SiteHeader } from "./hail-header/SiteHeader";
import { ThemeButton } from "./hail-footer/ThemeToggle";

export function Header() {
  return (
    <SiteHeader
      active="product"
      origin={SITE_ORIGIN}
      themeControl={<ThemeButton />}
    />
  );
}
