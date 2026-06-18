import assert from "node:assert/strict";
import test from "node:test";
import { parseSaiWalletPayload, walletQuickPickItems } from "../src/wallet";

test("parses the update block from the wallet payload", () => {
  const snapshot = parseSaiWalletPayload({
    balance: 1,
    update: { available: true, current: "0.2.3", latest: "0.2.4" }
  });
  assert.deepEqual(snapshot.update, { available: true, current: "0.2.3", latest: "0.2.4" });
});

test("treats a missing or malformed update block as no update", () => {
  assert.equal(parseSaiWalletPayload({ balance: 1 }).update, undefined);
  assert.equal(parseSaiWalletPayload({ balance: 1, update: "nope" }).update, undefined);

  // A block with no `available: true` is parsed but reports not-available, so a
  // partial/garbled payload can never read as "update ready".
  assert.deepEqual(parseSaiWalletPayload({ balance: 1, update: { latest: "0.2.4" } }).update, {
    available: false,
    current: undefined,
    latest: "0.2.4"
  });
});

test("surfaces an update item at the top of the wallet quick pick", () => {
  const snapshot = parseSaiWalletPayload({
    balance: 1,
    update: { available: true, current: "0.2.3", latest: "0.2.4" }
  });
  const items = walletQuickPickItems(snapshot);
  assert.match(items[0].label, /Update available: 0\.2\.3 → 0\.2\.4/);
  assert.match(items[0].description ?? "", /Install \/ Update CLI/);
});

test("omits the update item when no update is available", () => {
  const items = walletQuickPickItems(parseSaiWalletPayload({ balance: 1, update: { available: false } }));
  assert.equal(items.every((item) => !/Update available/.test(item.label)), true);
});
