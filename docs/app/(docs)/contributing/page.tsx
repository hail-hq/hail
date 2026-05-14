import Contributing from '../../../contributing.md';

export const metadata = {
  title: 'Contributing — Hail Docs',
  description: 'How to set up a dev environment, run the services, regenerate openapi.yaml, and contribute.',
};

export default function ContributingPage() {
  return (
    <article className="prose wrap">
      <div className="prose-eyebrow">DOCS · CONTRIBUTING</div>
      <Contributing />
    </article>
  );
}
