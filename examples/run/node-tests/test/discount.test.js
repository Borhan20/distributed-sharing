const test = require("node:test");
const assert = require("node:assert/strict");
const { discount, total } = require("../cart.js");

test("no code, no discount", () => {
  assert.equal(discount(100), 0);
});

test("SAVE10 takes 10 percent off", () => {
  assert.equal(total([{ units: 4, price: 25 }], "SAVE10"), 90);
});

test("an unknown code is an error", () => {
  assert.throws(() => discount(100, "FREE"), /unknown code/);
});
