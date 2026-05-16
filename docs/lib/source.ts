import { docs } from '@/.source/server';
import { loader } from 'fumadocs-core/source';

// baseUrl stays '/' because Next.js basePath ('/docs') prepends at render time;
// setting it to '/docs' here would double-prefix.
export const source = loader({
  baseUrl: '/',
  source: docs.toFumadocsSource(),
});
