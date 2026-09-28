# Cross-zone site header

This adapts the merged hail-website `MarketingHeader` and its CSS (September 2026) for the costs and docs zones: native absolute anchors cross Next.js
boundaries, an active destination identifies the current zone, and each app
supplies its own theme control. Docs uses Fumadocs' theme store; costs uses the
shared website theme store. Both persist the `hail-theme` preference.

Edit the header files here, then run from the repo root:

```sh
node scripts/sync-site-header.mjs
node scripts/sync-site-header.mjs --check
```

The docs copies are generated. When the website's navigation changes, compare
its `app/components/MarketingHeader.tsx` and CSS with this adaptation. Preserve
absolute marketing links and docs' local sidebar/search navigation.

Footers remain owned by hail-website. Sync those with its
`scripts/sync-footer.mjs --hail=/path/to/hail` and verify with `--check`.
