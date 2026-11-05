type SocialAccount = {
  label: string;
  href: string | null;
  handle?: string;
  publishing: "active" | "manual" | "none";
  showOnWebsite: boolean;
};

/** Posting accounts and public Hail profiles. A null URL means the profile link is not confirmed. */
export const SOCIAL_ACCOUNTS = {
  github: {
    label: "GitHub",
    href: "https://github.com/hail-hq/hail",
    publishing: "none",
    showOnWebsite: true,
  },
  x: {
    label: "X",
    href: "https://x.com/hail_hq",
    handle: "@hail_hq",
    publishing: "active",
    showOnWebsite: true,
  },
  youtube: {
    label: "YouTube",
    href: "https://www.youtube.com/@hail_so",
    handle: "@hail_so",
    publishing: "active",
    showOnWebsite: true,
  },
  tiktok: {
    label: "TikTok",
    href: "https://www.tiktok.com/@hail.so",
    publishing: "active",
    showOnWebsite: true,
  },
  instagram: {
    label: "Instagram",
    href: "https://www.instagram.com/hail.so_",
    publishing: "active",
    showOnWebsite: true,
  },
  facebook: {
    label: "Facebook",
    href: "https://www.facebook.com/hailhq",
    publishing: "active",
    showOnWebsite: true,
  },
  linkedin: {
    label: "LinkedIn",
    href: "https://www.linkedin.com/company/hail-hq",
    publishing: "active",
    showOnWebsite: true,
  },
  bluesky: {
    label: "Bluesky (personal)",
    href: "https://bsky.app/profile/r13i.bsky.social",
    publishing: "active",
    showOnWebsite: false,
  },
  reddit: {
    label: "Reddit",
    href: null,
    publishing: "manual",
    showOnWebsite: false,
  },
  threads: {
    label: "Threads",
    href: null,
    publishing: "none",
    showOnWebsite: false,
  },
} as const satisfies Record<string, SocialAccount>;

/** Confirmed public profiles only. Personal posting accounts never appear on the website. */
export const PUBLIC_SOCIAL_ACCOUNTS = Object.values(SOCIAL_ACCOUNTS).flatMap(
  ({ label, href, publishing, showOnWebsite }) => href && showOnWebsite ? [{ label, href, publishing }] : [],
);
