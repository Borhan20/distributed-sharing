// A small shopping cart: the code under test.
function lineTotal(item) {
  if (item.units < 0) throw new RangeError("units must not be negative");
  return item.units * item.price;
}

function subtotal(items) {
  return items.reduce((sum, item) => sum + lineTotal(item), 0);
}

function discount(amount, code) {
  const codes = { SAVE10: 0.1, HALF: 0.5 };
  if (code === undefined) return 0;
  if (!(code in codes)) throw new Error(`unknown code ${code}`);
  return Math.round(amount * codes[code] * 100) / 100;
}

function total(items, code) {
  const sub = subtotal(items);
  return Math.round((sub - discount(sub, code)) * 100) / 100;
}

module.exports = { lineTotal, subtotal, discount, total };
