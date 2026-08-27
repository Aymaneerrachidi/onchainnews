import assert from "node:assert/strict";
import test from "node:test";

import { mergeEditorialState, runnerKey } from "../api/editorial-store.mjs";

function snapshot() {
  const rows = [
    { chain: "solana", mint: "mint-a", symbol: "AAA", lore: "Original A" },
    { chain: "base", mint: "0xbbb", symbol: "BBB", lore: "Original B" },
  ];
  return {
    generatedAt: "2026-08-27T09:00:00Z",
    runnerUniverse: rows,
    runners: rows,
    discordPublishedRunners: rows,
    recap: { all: rows, highlighted: [rows[0]] },
    summary: { runnerCount: 2 },
  };
}

test("runnerKey is stable across chain and contract casing", () => {
  assert.equal(runnerKey({ chain: "Base", mint: "0xAbC" }), "base:0xabc");
});

test("editorial corrections propagate to every published collection", () => {
  const base = snapshot();
  const state = {
    reportId: base.generatedAt,
    hidden: ["base:0xbbb"],
    overrides: { "solana:mint-a": { lore: "Corrected A" } },
    added: [{ chain: "bnb", mint: "0xccc", symbol: "CCC", lore: "Added C" }],
    updatedAt: "2026-08-27T10:00:00Z",
  };

  const merged = mergeEditorialState(base, state);
  for (const rows of [merged.runnerUniverse, merged.runners, merged.discordPublishedRunners, merged.recap.all]) {
    assert.deepEqual(rows.map((row) => row.symbol), ["AAA", "CCC"]);
    assert.equal(rows[0].lore, "Corrected A");
  }
  assert.deepEqual(merged.recap.highlighted.map((row) => row.symbol), ["AAA"]);
  assert.equal(merged.summary.runnerCount, 2);
  assert.equal(base.runnerUniverse[0].lore, "Original A");
});

test("corrections automatically expire when a new scan is generated", () => {
  const base = snapshot();
  const stale = { reportId: "older-report", overrides: { "solana:mint-a": { lore: "Stale" } } };
  assert.equal(mergeEditorialState(base, stale), base);
});
