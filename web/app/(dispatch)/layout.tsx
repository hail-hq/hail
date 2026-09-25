import type { ReactNode } from "react";
import { Header } from "@/components/header";
import { Footer } from "@/components/footer";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <>
      <Header />
      {children}
      <p
        className="wrap"
        style={{ paddingBlock: 20, fontSize: 12, color: "var(--color-mute)" }}
      >
        Hail costs · CC-BY-4.0 ·{" "}
        <a href="https://github.com/hail-hq/hail/tree/main/costs">
          Dataset source
        </a>
      </p>
      <Footer />
    </>
  );
}
