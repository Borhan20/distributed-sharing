// Prints the license of every direct dependency (from node_modules/<pkg>/package.json)
// and fails if one is not on the allowlist.
import { readFileSync } from "node:fs";

const ALLOWED = new Set(["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD"]);
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url)));
let bad = 0;
for (const name of [...Object.keys(pkg.dependencies), ...Object.keys(pkg.devDependencies)]) {
  const meta = JSON.parse(readFileSync(new URL(`../node_modules/${name}/package.json`, import.meta.url)));
  const lic = typeof meta.license === "string" ? meta.license : JSON.stringify(meta.license);
  const ok = ALLOWED.has(lic);
  if (!ok) bad++;
  console.log(`${ok ? "ok  " : "FLAG"} ${name}@${meta.version} ${lic}`);
}
process.exit(bad ? 1 : 0);
