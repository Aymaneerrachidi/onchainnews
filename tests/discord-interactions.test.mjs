import assert from "node:assert/strict";
import test from "node:test";

import {
  applyLiveCaps,
  contractsFromEmbeds,
  dexChain,
  fetchLiveCaps,
  fetchRunnerSnapshot,
  filterRunnerRows,
  resetRefreshStateForTests,
  securityEligible,
} from "../api/discord-interactions.mjs";


test("Fomo chain names map to Dexscreener chain names", () => {
  assert.equal(dexChain("bnb"), "bsc");
  assert.equal(dexChain("solana"), "solana");
  assert.equal(dexChain("base"), "base");
});


test("contract extraction deduplicates exact Fomo links", () => {
  const url = "https://fomo.family/tokens/solana/MINT";
  const contracts = contractsFromEmbeds([{
    description: `[**$ONE**](${url}) → **hit $2M**\n[duplicate](${url})`,
  }]);

  assert.equal(contracts.length, 1);
  assert.deepEqual(contracts[0], { chain: "solana", mint: "MINT", url });
});


test("BNB refresh uses BSC and caches the deepest exact market", async () => {
  resetRefreshStateForTests();
  let calls = 0;
  const contract = {
    chain: "bnb",
    mint: "0xAbC",
    url: "https://fomo.family/tokens/bnb/0xAbC",
  };
  const fetchStub = async (url) => {
    calls += 1;
    assert.match(String(url), /\/tokens\/v1\/bsc\/0xAbC$/);
    return new Response(JSON.stringify([
      { baseToken: { address: "0xAbC" }, marketCap: 1_000_000, liquidity: { usd: 20_000 } },
      { baseToken: { address: "0xabc" }, marketCap: 1_250_000, liquidity: { usd: 100_000 } },
      { baseToken: { address: "wrong" }, marketCap: 99_000_000, liquidity: { usd: 9_000_000 } },
    ]), { status: 200, headers: { "content-type": "application/json" } });
  };

  const first = await fetchLiveCaps([contract], fetchStub, 1_000);
  const second = await fetchLiveCaps([contract], fetchStub, 10_000);

  assert.equal(first.get("bnb:0xabc"), 1_250_000);
  assert.equal(second.get("bnb:0xabc"), 1_250_000);
  assert.equal(calls, 1);
});


test("live cap replaces both current and legacy separators", () => {
  const contract = {
    chain: "solana",
    mint: "MINT",
    url: "https://fomo.family/tokens/solana/MINT",
  };
  const prices = new Map([["solana:mint", 2_500_000]]);

  const clean = applyLiveCaps(
    `[**$ONE**](${contract.url}) → **hit $4M · now $1M**`,
    [contract],
    prices,
  );
  const legacy = applyLiveCaps(
    `[**$ONE**](${contract.url}) → **hit $4M Ã‚Â· now $1M**`,
    [contract],
    prices,
  );

  assert.match(clean, /hit \$4M · now \$2\.5M/);
  assert.match(legacy, /hit \$4M · now \$2\.5M/);
  assert.doesNotMatch(legacy, /Ã‚Â·/);
});


function safeRunner(overrides = {}) {
  return {
    symbol: "RUN",
    mint: "mint-1",
    chain: "solana",
    marketCap: 300_000,
    peakMarketCap: 420_000,
    liquidity: 60_000,
    holders: 900,
    top10Pct: 18,
    lpLockedPct: 100,
    mintAuthorityRenounced: true,
    freezeAuthorityDisabled: true,
    rugged: false,
    securityFlags: [],
    volume24h: 900_000,
    ...overrides,
  };
}


test("runner filters combine chain and verified 24h peak bands", () => {
  const snapshot = {
    runnerUniverse: [
      safeRunner(),
      safeRunner({ symbol: "MID", mint: "mint-2", peakMarketCap: 750_000 }),
      safeRunner({ symbol: "BASE", mint: "0xbase", chain: "base", peakMarketCap: 2_000_000 }),
    ],
  };

  assert.deepEqual(
    filterRunnerRows(snapshot, "solana", "250k-500k").map((row) => row.symbol),
    ["RUN"],
  );
  assert.deepEqual(
    filterRunnerRows(snapshot, "all", "500k-1m").map((row) => row.symbol),
    ["MID"],
  );
  assert.deepEqual(
    filterRunnerRows(snapshot, "base", "1m-10m").map((row) => row.symbol),
    ["BASE"],
  );
});


test("Discord rejects confirmed danger without treating missing legacy data as a rug", () => {
  assert.equal(securityEligible(safeRunner()), true);
  assert.equal(securityEligible(safeRunner({ top10Pct: 31 })), false);
  assert.equal(securityEligible(safeRunner({ top10Pct: null })), true);
  assert.equal(securityEligible(safeRunner({ holders: 0 })), true);
  assert.equal(securityEligible(safeRunner({ lpLockedPct: 0 })), false);
  assert.equal(securityEligible(safeRunner({ mintAuthorityRenounced: false })), false);
  assert.equal(securityEligible(safeRunner({
    providerEvidence: { gmgn: { isHoneypot: 1 } },
  })), false);
  assert.equal(securityEligible(safeRunner({
    providerEvidence: { gmgn: { devTeamHoldRate: 0.16 } },
  })), false);
  assert.equal(securityEligible(safeRunner({
    providerEvidence: { gmgn: { washTrading: true } },
  })), false);
});


test("runner snapshot is cached for a click burst", async () => {
  resetRefreshStateForTests();
  let calls = 0;
  const fetchStub = async () => {
    calls += 1;
    return new Response(JSON.stringify({ runnerUniverse: [safeRunner()] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const first = await fetchRunnerSnapshot("https://app.test/data/latest.json", fetchStub, 1_000);
  const second = await fetchRunnerSnapshot("https://app.test/data/latest.json", fetchStub, 20_000);

  assert.equal(first.runnerUniverse.length, 1);
  assert.equal(second.runnerUniverse.length, 1);
  assert.equal(calls, 1);
});
