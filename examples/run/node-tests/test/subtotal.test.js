const test = require("node:test");
const assert = require("node:assert/strict");
const { subtotal } = require("../cart.js");

test("an empty cart costs nothing", () => {
  assert.equal(subtotal([]), 0);
});

test("lines are added up", () => {
  assert.equal(subtotal([{ units: 2, price: 3 }, { units: 1, price: 4 }]), 10);
});
