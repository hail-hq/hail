type SocialAccount = {
  label: string;
  href: string | null;
  handle?: string;
  publishing: "active" | "manual" | "none";
};

/** Hail-owned accounts. A null URL means the owner has not confirmed the profile link. */
export const SOCIAL_ACCOUNTS = {
  github: {
    label: "GitHub",
    href: "https://github.com/hail-hq/hail",
    publishing: "none",
  },
  x: {
    label: "X",
    href: "https://x.com/hail_hq",
    handle: "@hail_hq",
    publishing: "active",
  },
  youtube: {
    label: "YouTube",
    href: null,
    publishing: "active",
  },
  tiktok: {
    label: "TikTok",
    href: "https://www.tiktok.com/@hail.so",
    publishing: "active",
  },
  instagram: {
    label: "Instagram",
    href: "https://www.instagram.com/hail.so_",
    publishing: "active",
  },
  facebook: {
    label: "Facebook",
    href: "https://www.facebook.com/hailhq",
    publishing: "active",
  },
  linkedin: {
    label: "LinkedIn",
    href: "https://www.linkedin.com/company/hail-hq",
    publishing: "active",
  },
  bluesky: {
    label: "Bluesky",
    href: null,
    publishing: "active",
  },
  reddit: {
    label: "Reddit",
    href: null,
    publishing: "manual",
  },
  threads: {
    label: "Threads",
    href: null,
    publishing: "none",
  },
} as const satisfies Record<string, SocialAccount>;

/** Confirmed links for public navigation and organization identity. Never guess a missing URL. */
export const PUBLIC_SOCIAL_ACCOUNTS = Object.values(SOCIAL_ACCOUNTS).flatMap(
  ({ label, href, publishing }) => href ? [{ label, href, publishing }] : [],
);
