const test = require("node:test");
const assert = require("node:assert/strict");
const { lineTotal } = require("../cart.js");

test("a line is units times price", () => {
  assert.equal(lineTotal({ units: 3, price: 2.5 }), 7.5);
});

test("negative units are refused", () => {
  assert.throws(() => lineTotal({ units: -1, price: 1 }), RangeError);
});
