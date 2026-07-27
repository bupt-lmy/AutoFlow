#!/usr/bin/env node

import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDir, "../../../..");
const requireFromFrontend = createRequire(
  pathToFileURL(path.join(repositoryRoot, "frontend/package.json")),
);
const { chromium } = requireFromFrontend("playwright");

function fail(message) {
  throw new Error(message);
}

function insideRepository(candidate) {
  const relative = path.relative(repositoryRoot, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function localUrl(value) {
  const parsed = new URL(value);
  const localHosts = new Set(["127.0.0.1", "localhost", "::1"]);
  if (!localHosts.has(parsed.hostname) && process.env.AXONFLOW_BROWSER_ALLOW_REMOTE !== "1") {
    fail(`Remote browser target is disabled: ${parsed.hostname}`);
  }
  if (!new Set(["http:", "https:"]).has(parsed.protocol)) {
    fail(`Unsupported URL protocol: ${parsed.protocol}`);
  }
  return parsed.toString();
}

function planArgument(argv) {
  const index = argv.indexOf("--plan");
  if (index < 0 || !argv[index + 1]) fail("Usage: browser_acceptance.mjs --plan <plan.json>");
  return argv[index + 1];
}

async function unique(locator, description) {
  const count = await locator.count();
  if (count !== 1) fail(`${description} matched ${count} elements; expected exactly one`);
  return locator;
}

async function launchBrowser() {
  const channel = process.env.AXONFLOW_BROWSER_CHANNEL || "chrome";
  try {
    return await chromium.launch({ channel, headless: true });
  } catch (channelError) {
    try {
      return await chromium.launch({ headless: true });
    } catch (bundledError) {
      fail(
        `Unable to launch Chromium channel '${channel}' or bundled browser: ` +
          `${channelError.message}; ${bundledError.message}`,
      );
    }
  }
}

async function runAction(page, action, timeoutMs, evidence) {
  if (!action || typeof action !== "object") fail("Every action must be an object");
  const type = action.type;

  if (type === "expect_text" || type === "wait_for_text") {
    if (typeof action.text !== "string" || !action.text) fail(`${type} requires text`);
    const locator = page.getByText(action.text, { exact: action.exact === true });
    await locator.first().waitFor({ state: "visible", timeout: timeoutMs });
    evidence.push({ type, text: action.text, status: "passed" });
    return;
  }

  if (type === "click") {
    if (!action.role || !action.name) fail("click requires role and name");
    const locator = await unique(
      page.getByRole(action.role, { name: action.name, exact: true }),
      `click ${action.role} '${action.name}'`,
    );
    await locator.click({ timeout: timeoutMs });
    evidence.push({ type, role: action.role, name: action.name, status: "passed" });
    return;
  }

  if (type === "fill") {
    if (!action.label || typeof action.value !== "string") fail("fill requires label and value");
    const locator = await unique(
      page.getByLabel(action.label, { exact: true }),
      `fill '${action.label}'`,
    );
    await locator.fill(action.value, { timeout: timeoutMs });
    evidence.push({ type, label: action.label, status: "passed" });
    return;
  }

  if (type === "press") {
    if (!action.label || !action.key) fail("press requires label and key");
    const locator = await unique(
      page.getByLabel(action.label, { exact: true }),
      `press '${action.label}'`,
    );
    await locator.press(action.key, { timeout: timeoutMs });
    evidence.push({ type, label: action.label, key: action.key, status: "passed" });
    return;
  }

  if (type === "screenshot") {
    if (typeof action.path !== "string" || !action.path) fail("screenshot requires path");
    const screenshotPath = path.resolve(repositoryRoot, action.path);
    if (!insideRepository(screenshotPath)) fail("Screenshot path must stay inside the repository");
    await fs.mkdir(path.dirname(screenshotPath), { recursive: true });
    await page.screenshot({ path: screenshotPath, fullPage: action.full_page !== false });
    evidence.push({
      type,
      path: path.relative(repositoryRoot, screenshotPath),
      status: "passed",
    });
    return;
  }

  fail(`Unsupported browser action: ${String(type)}`);
}

const result = { status: "failed", url: null, actions: [], error: null };
let browser;

try {
  const requestedPlan = path.resolve(repositoryRoot, planArgument(process.argv.slice(2)));
  if (!insideRepository(requestedPlan)) fail("Plan path must stay inside the repository");
  const plan = JSON.parse(await fs.readFile(requestedPlan, "utf8"));
  result.url = localUrl(plan.url);
  const timeoutMs = Number(plan.timeout_ms || 15000);
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1000 || timeoutMs > 120000) {
    fail("timeout_ms must be between 1000 and 120000");
  }
  if (!Array.isArray(plan.actions) || plan.actions.length === 0) {
    fail("Plan actions must be a non-empty array");
  }

  browser = await launchBrowser();
  const page = await browser.newPage();
  await page.goto(result.url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
  for (const action of plan.actions) {
    await runAction(page, action, timeoutMs, result.actions);
  }
  result.status = "passed";
} catch (error) {
  result.error = error instanceof Error ? error.message : String(error);
} finally {
  if (browser) await browser.close();
}

console.log(JSON.stringify(result, null, 2));
process.exitCode = result.status === "passed" ? 0 : 1;
