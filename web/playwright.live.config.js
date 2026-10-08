import { defineConfig } from "@playwright/test";
import path from "node:path";

const configured = Boolean(process.env.KNOWLEDGE_LIVE_URL);
const baseURL = process.env.KNOWLEDGE_LIVE_URL || "http://127.0.0.1:1";
const url = new URL(baseURL);
if (
  !["http:", "https:"].includes(url.protocol) ||
  !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
  url.username ||
  url.password ||
  url.pathname !== "/" ||
  url.search ||
  url.hash
) {
  throw new Error(
    "Live acceptance only supports a loopback HTTP(S) origin without embedded credentials.",
  );
}
if (
  configured &&
  [
    "KNOWLEDGE_LIVE_USERNAME",
    "KNOWLEDGE_LIVE_PASSWORD",
    "KNOWLEDGE_LIVE_DOCUMENT_ID",
  ].some((key) => !process.env[key])
) {
  throw new Error(
    "Live acceptance requires an ephemeral admin username/password and an exact isolated document ID via environment.",
  );
}

export default defineConfig({
  testDir: "./tests",
  outputDir: path.join(
    process.env.KNOWLEDGE_TEST_OUTPUT_DIR || "./test-results",
    "live",
  ),
  testMatch: "**/*.live.spec.js",
  workers: 1,
  retries: 0,
  timeout: 120000,
  expect: { timeout: 15000 },
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    // Login credentials must never be captured in network traces or video.
    trace: "off",
    video: "off",
    screenshot: "only-on-failure",
  },
});
