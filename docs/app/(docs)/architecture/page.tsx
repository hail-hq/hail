import Architecture from '../../../architecture.md';

export const metadata = {
  title: 'Architecture — Hail Docs',
  description:
    'How Hail is structured: services, shared core, OpenAPI contract, MCP transport, and the v1 scope.',
};

export default function ArchitecturePage() {
  return (
    <article className="prose wrap">
      <div className="prose-eyebrow">DOCS · ARCHITECTURE</div>
      <Architecture />
    </article>
  );
}
