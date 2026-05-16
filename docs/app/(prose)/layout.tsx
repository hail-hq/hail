import type { ReactNode } from 'react';
import { RootProvider } from 'fumadocs-ui/provider/next';
import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import { source } from '@/lib/source';
import { Header } from '@/components/header';
import { Footer } from '@/components/footer';

export default function ProseLayout({ children }: { children: ReactNode }) {
  return (
    <RootProvider>
      <Header />
      <DocsLayout
        tree={source.pageTree}
        nav={{ enabled: false }}
        sidebar={{
          defaultOpenLevel: 1,
        }}
      >
        {children}
      </DocsLayout>
      <Footer />
    </RootProvider>
  );
}
