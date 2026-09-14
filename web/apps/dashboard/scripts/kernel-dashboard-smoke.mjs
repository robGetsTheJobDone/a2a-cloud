import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import process from "node:process";

const root = process.cwd();
const checks = [];

function pass(name, detail, payload = {}) {
  checks.push({ name, status: "passed", detail, ...payload });
}

function fail(name, detail, payload = {}) {
  checks.push({ name, status: "failed", detail, ...payload });
}

function assertFile(name, relativePath) {
  const fullPath = path.join(root, relativePath);
  if (existsSync(fullPath) && statSync(fullPath).isFile()) {
    pass(name, "file exists", { path: relativePath });
    return true;
  }
  fail(name, "file is missing", { path: relativePath });
  return false;
}

function read(relativePath) {
  return readFileSync(path.join(root, relativePath), "utf8");
}

function walk(relativeDir) {
  const fullDir = path.join(root, relativeDir);
  if (!existsSync(fullDir)) return [];
  const entries = [];
  for (const entry of readdirSync(fullDir)) {
    const relativeEntry = path.join(relativeDir, entry);
    const fullEntry = path.join(root, relativeEntry);
    if (statSync(fullEntry).isDirectory()) {
      entries.push(...walk(relativeEntry));
    } else {
      entries.push(relativeEntry);
    }
  }
  return entries;
}

function assertSourceWiring() {
  const sources = [
    ["kernel simulation component", "src/components/KernelSimulations.tsx"],
    ["kernel trace parser", "src/kernelTrace.ts"],
    ["chat evidence parser", "src/chatEvidence.ts"],
    ["api client", "src/api.ts"],
  ];
  for (const [name, relativePath] of sources) {
    assertFile(name, relativePath);
  }

  const packageJson = JSON.parse(read("package.json"));
  if (packageJson.scripts?.["smoke:kernel"] === "node scripts/kernel-dashboard-smoke.mjs") {
    pass("dashboard smoke package script", "smoke:kernel is registered");
  } else {
    fail("dashboard smoke package script", "smoke:kernel is not registered");
  }

  const apiSource = read("src/api.ts");
  const requiredApiFragments = [
    "/v1/me/kernel-simulations/templates",
    "/v1/me/kernel-simulations/runs",
    "/replay",
  ];
  const missingApiFragments = requiredApiFragments.filter((fragment) => !apiSource.includes(fragment));
  if (missingApiFragments.length === 0) {
    pass("dashboard kernel api wiring", "kernel template/run/replay endpoints are wired");
  } else {
    fail("dashboard kernel api wiring", "kernel API fragments are missing", { missing: missingApiFragments });
  }
}

function assertDistWiring() {
  if (!assertFile("dashboard dist index", "dist/index.html")) return;

  const indexHtml = read("dist/index.html");
  const assetRefs = [...indexHtml.matchAll(/src="([^"]+\.js)"/g)].map((match) => match[1]);
  if (assetRefs.length === 0) {
    fail("dashboard dist assets", "index.html does not reference a JavaScript asset");
    return;
  }
  pass("dashboard dist assets", "index.html references JavaScript assets", { count: assetRefs.length });

  const distFiles = walk("dist");
  const textFiles = distFiles.filter((file) => /\.(js|css|html)$/.test(file));
  const combined = textFiles.map((file) => read(file)).join("\n");
  const requiredFragments = [
    "kernel-simulations",
    "/v1/me/kernel-simulations/templates",
    "/v1/me/kernel-simulations/runs",
    "scenario_trace_recorded",
  ];
  const missingFragments = requiredFragments.filter((fragment) => !combined.includes(fragment));
  if (missingFragments.length === 0) {
    pass("dashboard kernel bundle wiring", "built assets include kernel simulation and evidence surfaces");
  } else {
    fail("dashboard kernel bundle wiring", "built assets are missing kernel fragments", { missing: missingFragments });
  }
}

async function assertRemoteDashboard() {
  const url = process.env.A2A_DASHBOARD_SMOKE_URL || process.env.A2A_SMOKE_DASHBOARD_URL;
  if (!url) {
    checks.push({
      name: "remote dashboard url",
      status: "skipped",
      detail: "A2A_DASHBOARD_SMOKE_URL was not set",
    });
    return;
  }
  try {
    const response = await fetch(url, { redirect: "follow" });
    if (!response.ok) {
      fail("remote dashboard url", "expected HTTP 200", { statusCode: response.status, url });
      return;
    }
    const text = await response.text();
    if (!text.includes("assets/") && !text.toLowerCase().includes("kernel")) {
      fail("remote dashboard url", "response does not look like the dashboard shell", { url });
      return;
    }
    pass("remote dashboard url", "dashboard shell responded", { statusCode: response.status, url });
  } catch (error) {
    fail("remote dashboard url", `request failed: ${error.message}`, { url });
  }
}

assertSourceWiring();
assertDistWiring();
await assertRemoteDashboard();

const failed = checks.filter((check) => check.status === "failed");
const passed = checks.filter((check) => check.status === "passed");
const skipped = checks.filter((check) => check.status === "skipped");
console.log(
  JSON.stringify(
    {
      passed: passed.length,
      failed: failed.length,
      skipped: skipped.length,
      checks,
    },
    null,
    2,
  ),
);
process.exit(failed.length === 0 ? 0 : 1);
