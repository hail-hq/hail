import { resolve } from "node:path";
import type { NextConfig } from "next";

const workspaceRoot = resolve(import.meta.dirname, "../..");
const config: NextConfig = {
  turbopack: { root: workspaceRoot },
  outputFileTracingRoot: workspaceRoot,
};
export default config;
