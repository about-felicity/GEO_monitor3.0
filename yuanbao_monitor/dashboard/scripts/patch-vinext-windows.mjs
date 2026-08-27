import fs from "node:fs";
import path from "node:path";

// vinext 0.0.50 stores StaticFileCache keys using path.relative(). On
// Windows that produces `assets\\file.js`, while browser requests use
// `/assets/file.js`, so every production JS/CSS request returns 404. Keep the
// dependency patch local, idempotent, and applied before every production
// start so npm reinstall cannot silently reintroduce the broken panel.
const target = path.resolve(
  "node_modules/vinext/dist/server/static-file-cache.js",
);
const before = "relativePath: path.relative(base, batch[j]),";
const after =
  'relativePath: path.relative(base, batch[j]).split(path.sep).join("/"),';

if (!fs.existsSync(target)) {
  throw new Error(`vinext runtime not found: ${target}`);
}

const source = fs.readFileSync(target, "utf8");
if (source.includes(after)) {
  process.exit(0);
}
if (!source.includes(before)) {
  throw new Error("vinext StaticFileCache layout changed; refusing an unsafe patch");
}
fs.writeFileSync(target, source.replace(before, after), "utf8");
