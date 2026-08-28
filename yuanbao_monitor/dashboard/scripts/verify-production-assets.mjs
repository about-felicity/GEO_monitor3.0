import fs from "node:fs";
import path from "node:path";

const configured = String(process.env.VITE_MONITOR_BASE_PATH || "/geo")
  .trim()
  .replace(/\/$/, "");
const expectedPrefix = `${configured}/assets/`;
const manifests = [
  path.resolve("dist/server/__vite_rsc_assets_manifest.js"),
  path.resolve("dist/server/ssr/__vite_rsc_assets_manifest.js"),
];

for (const manifest of manifests) {
  if (!fs.existsSync(manifest)) {
    throw new Error(`Production asset manifest is missing: ${manifest}`);
  }
  const source = fs.readFileSync(manifest, "utf8");
  if (!source.includes(expectedPrefix)) {
    throw new Error(
      `Production assets must use ${expectedPrefix}; refusing to publish an unstyled dashboard`,
    );
  }
  if (/(["'`(=])\/assets\//.test(source)) {
    throw new Error(
      "Root /assets references detected; refusing to publish an unstyled dashboard",
    );
  }
}

const clientAssets = path.resolve("dist/client/assets");
if (!fs.existsSync(clientAssets)) {
  throw new Error(`Production client assets are missing: ${clientAssets}`);
}
const emitted = fs.readdirSync(clientAssets);
if (!emitted.some((name) => name.endsWith(".css")) || !emitted.some((name) => name.endsWith(".js"))) {
  throw new Error("Production CSS or JavaScript assets are missing");
}

console.log(`Production asset verification passed: ${expectedPrefix}`);
