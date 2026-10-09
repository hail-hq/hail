# Cross-zone site header

These files are generated. Do not edit them here. hail-website owns the header
in `app/components/MarketingHeader.tsx` and generates the costs and docs copies
with its sync script. The script turns Next.js links into absolute anchors, so
the links cross app boundaries, and takes the theme control from each app. Docs
uses the Fumadocs theme store. Costs uses the shared website theme store. Both
persist the `hail-theme` preference.

Run from the hail-website checkout:

```sh
node scripts/sync-site-header.mjs --hail=/path/to/hail
node scripts/sync-site-header.mjs --hail=/path/to/hail --check
```

The footer works the same way with `scripts/sync-footer.mjs`.
