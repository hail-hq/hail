import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { after, before, test } from "node:test";
import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const { models } = JSON.parse(
  await readFile(new URL("../../costs/llm.json", import.meta.url), "utf8"),
);
const selected = models.slice(0, 2);
let server;
let browser;
let origin;

before(async () => {
  server = spawn(
    process.execPath,
    [
      require.resolve("next/dist/bin/next"),
      "start",
      "--hostname",
      "127.0.0.1",
      "--port",
      "0",
    ],
    { cwd: new URL("../", import.meta.url) },
  );
  origin = await new Promise((resolve, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error(output)), 20_000);
    server.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    server.once("exit", () => {
      clearTimeout(timeout);
      reject(new Error(output));
    });
    const collect = (chunk) => {
      output += chunk;
      if (output.includes("Ready")) {
        clearTimeout(timeout);
        resolve(output.match(/http:\/\/127\.0\.0\.1:\d+/)[0]);
      }
    };
    server.stdout.on("data", collect);
    server.stderr.on("data", collect);
  });
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
  if (server && server.exitCode === null && server.signalCode === null) {
    server.kill();
    await once(server, "exit");
  }
});

test("shared model selections open without hydration errors", async () => {
  const page = await browser.newPage({ locale: "sv-SE" });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    const ids = selected.map((model) => model.model_id).join(",");
    await page.goto(`${origin}/costs/compare?m=${encodeURIComponent(ids)}`);
    await page.getByRole("button", { name: "Clear", exact: true }).waitFor();
    assert.match(
      await page.locator(".dispatch-tape").innerText(),
      /2 of 6 slots/,
    );
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      selected.map((model) => model.display_name),
    );
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    await page.getByText("No models selected", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});

test("a comparison opened after the build day hydrates without stale date text", async () => {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    const later = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
    await page.clock.setFixedTime(later);
    await page.goto(`${origin}/costs/compare`);
    await page.getByText("No models selected", { exact: true }).waitFor();
    assert.ok(
      (await page.locator(".dispatch-tape").innerText()).includes(
        later.toISOString().slice(0, 10),
      ),
    );
    await page.locator("button.add-pill").first().click();
    await page.getByRole("button", { name: "Clear", exact: true }).waitFor();
    assert.match(
      await page.locator(".dispatch-tape").innerText(),
      /1 of 6 slots/,
    );
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});

test("products sharing a provider model ID can be added, shared and removed independently", async () => {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto(`${origin}/costs/compare`);
    const realtime = page.locator(
      'button.add-pill[data-model-key="stt:AssemblyAI:universal-3-5-pro:streaming-realtime"]',
    );
    await realtime.click();
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      ["Universal-3.5 Pro Realtime"],
    );
    assert.match(await page.locator(".compare-table").innerText(), /0\.007500/);
    assert.equal(await realtime.count(), 0);
    await page
      .locator(
        'button.add-pill[data-model-key="stt:AssemblyAI:universal-3-5-pro:batch-standard"]',
      )
      .click();
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      ["Universal-3.5 Pro Realtime", "Universal-3.5 Pro"],
    );
    const shared = page.url();
    await page.goto(shared);
    await page
      .getByRole("link", {
        name: "Remove Universal-3.5 Pro Realtime from comparison",
        exact: true,
      })
      .waitFor();
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      ["Universal-3.5 Pro Realtime", "Universal-3.5 Pro"],
    );
    await page
      .getByRole("link", {
        name: "Remove Universal-3.5 Pro Realtime from comparison",
        exact: true,
      })
      .click();
    await page
      .getByRole("link", {
        name: "Remove Universal-3.5 Pro from comparison",
        exact: true,
      })
      .waitFor();
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      ["Universal-3.5 Pro"],
    );
    await page
      .getByRole("link", {
        name: "Remove Universal-3.5 Pro from comparison",
        exact: true,
      })
      .click();
    await page.getByText("No models selected", { exact: true }).waitFor();
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});

test("legacy bare model IDs still select the original product", async () => {
  const page = await browser.newPage();
  try {
    await page.goto(`${origin}/costs/compare?m=universal-3-5-pro`);
    await page
      .getByRole("link", {
        name: "Remove Universal-3.5 Pro from comparison",
        exact: true,
      })
      .waitFor();
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      ["Universal-3.5 Pro"],
    );
  } finally {
    await page.close();
  }
});

test("the costs table comparison link selects the Realtime product", async () => {
  const page = await browser.newPage();
  try {
    await page.goto(`${origin}/costs`);
    await page
      .locator(
        'a.compare-link[href*="universal-3-5-pro"][href*="streaming-realtime"]',
      )
      .click();
    await page
      .getByRole("link", {
        name: "Remove Universal-3.5 Pro Realtime from comparison",
        exact: true,
      })
      .waitFor();
    assert.deepEqual(
      await page.locator(".compare-model-name").allTextContents(),
      ["Universal-3.5 Pro Realtime"],
    );
  } finally {
    await page.close();
  }
});
