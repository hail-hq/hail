/** Build first with `pnpm check:structured-data`; this runner owns its servers. */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const children = [];

function start(command, args, env = process.env) {
  const child = spawn(command, args, {
    cwd: root,
    env,
    stdio: ["ignore", "pipe", "inherit"],
    detached: process.platform !== "win32",
  });
  children.push(child);
  return child;
}

function terminate(child, signal) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null)
    return;
  try {
    if (process.platform === "win32") child.kill(signal);
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
}

async function stop(child) {
  if (!child.pid || child.exitCode !== null || child.signalCode !== null)
    return;
  const closed = once(child, "close");
  terminate(child, "SIGTERM");
  const force = setTimeout(() => terminate(child, "SIGKILL"), 5000);
  try {
    await closed;
  } finally {
    clearTimeout(force);
  }
}

async function startApp(app, zone) {
  // Port zero lets the OS assign a port to this child, avoiding stale servers.
  const child = start(
    process.execPath,
    [
      `${app}/node_modules/next/dist/bin/next`,
      "start",
      app,
      "--hostname",
      "127.0.0.1",
      "--port",
      "0",
    ],
    { ...process.env, NO_COLOR: "1" },
  );
  const origin = await new Promise((resolve, reject) => {
    const timeout = setTimeout(
      () => reject(new Error(`${app} did not announce its port`)),
      30000,
    );
    let output = "";
    child.stdout.on("data", (data) => {
      process.stdout.write(data);
      output = (output + data).slice(-8192);
      const match = output.match(/Local:\s+(http:\/\/127\.0\.0\.1:\d+)/);
      if (match) {
        clearTimeout(timeout);
        resolve(match[1]);
      }
    });
    child.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`${app} exited before serving its sitemap (${code})`));
    });
  });
  for (let attempt = 0; attempt < 100; attempt++) {
    if (child.exitCode !== null || child.signalCode !== null)
      throw new Error(`${app} exited during startup`);
    try {
      const response = await fetch(`${origin}/${zone}/sitemap.xml`, {
        signal: AbortSignal.timeout(2000),
      });
      if (response.ok && (await response.text()).includes("<urlset"))
        return origin;
    } catch {}
    await delay(200);
  }
  throw new Error(`${app} did not serve its sitemap`);
}

const interrupt = () => {
  process.exitCode = 1;
  for (const child of children) terminate(child, "SIGTERM");
};
process.once("SIGINT", interrupt);
process.once("SIGTERM", interrupt);

try {
  const [docs, costs] = await Promise.all([
    startApp("docs-site", "docs"),
    startApp("web", "costs"),
  ]);
  const tests = start("pnpm", ["run", "test:structured-data"], {
    ...process.env,
    STRUCTURED_DATA_REQUIRED: "1",
    DOCS_TEST_ORIGIN: docs,
    COSTS_TEST_ORIGIN: costs,
  });
  let output = "";
  tests.stdout.on("data", (data) => {
    output += data;
    process.stdout.write(data);
  });
  const [code] = await once(tests, "close");
  assert.equal(code, 0, "Structured data tests failed");
  const skipped = [...output.matchAll(/^# skipped (\d+)$/gm)].at(-1)?.[1];
  assert.equal(
    skipped,
    "0",
    "The automatic schema check must report zero skipped tests",
  );
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await Promise.all(children.map(stop));
  process.removeListener("SIGINT", interrupt);
  process.removeListener("SIGTERM", interrupt);
}
