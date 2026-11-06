// Summarise a small, made-up sales dataset: totals per region and per product.
// Each part takes every n-th record (POOL_TASK_INDEX of POOL_TASK_COUNT); one part does all of them.
// Writes /out/result.json (one row of the results table) and /out/by_region.csv.
const fs = require("fs");

const part = Number(process.env.POOL_TASK_INDEX || 0);
const parts = Number(process.env.POOL_TASK_COUNT || 1);
const records = JSON.parse(fs.readFileSync("/app/sales.json", "utf8")).filter((_, i) => i % parts === part);

const byRegion = {};
let total = 0;
for (const r of records) {
  const amount = r.units * r.price;
  total += amount;
  byRegion[r.region] = byRegion[r.region] || { orders: 0, units: 0, amount: 0 };
  byRegion[r.region].orders += 1;
  byRegion[r.region].units += r.units;
  byRegion[r.region].amount += amount;
}

const rows = ["region,orders,units,amount"];
for (const [region, s] of Object.entries(byRegion).sort(([a], [b]) => a.localeCompare(b))) {
  rows.push(`${region},${s.orders},${s.units},${s.amount.toFixed(2)}`);
  console.log(`${region.padEnd(6)} ${String(s.orders).padStart(4)} orders  ${s.amount.toFixed(2).padStart(10)}`);
}
fs.writeFileSync("/out/by_region.csv", rows.join("\n") + "\n");

const result = { part, records: records.length, total: Number(total.toFixed(2)),
                 average_order: Number((total / Math.max(records.length, 1)).toFixed(2)) };
fs.writeFileSync("/out/result.json", JSON.stringify(result));
console.log(`done: ${records.length} records, total ${result.total}`);
