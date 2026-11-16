import type { ReactNode } from "react";
import { Header } from "@/components/header";
import { Footer } from "@/components/footer";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <div className="hail-public hail-costs">
      <Header />
      {children}
      <p className="wrap costs-license">
        Hail costs · CC-BY-4.0 ·{" "}
        <a href="https://github.com/hail-hq/hail/tree/main/costs">
          Dataset source
        </a>
      </p>
      <Footer />
    </div>
  );
}
