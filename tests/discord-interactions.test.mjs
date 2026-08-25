import assert from "node:assert/strict";
import test from "node:test";

import {
  activityEligible,
  applyLiveCaps,
  contractsFromEmbeds,
  dexChain,
  fetchLiveCaps,
  fetchRunnerSnapshot,
  filterRunnerRows,
  filterComponents,
  publicComponents,
  renderFilterResponse,
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
    ageHours: 12,
    scores: { runner: 70, organic: 70, manipulation: 10 },
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


test("Discord controls use balanced rows and visible filter state", () => {
  const initial = publicComponents(1234, "20260825");
  assert.deepEqual(initial.slice(0, 2).map((row) => row.components.length), [3, 3]);
  assert.deepEqual(initial[0].components.map((button) => button.label), ["All", "Solana", "BNB"]);
  assert.deepEqual(initial[1].components.map((button) => button.label), ["Base", "Ethereum", "Robinhood"]);
  assert.equal(initial[0].components[0].style, 1);
  assert.equal(initial[2].components[0].style, 1);
  assert.equal(initial[3].components[1].disabled, true);

  const filtered = filterComponents("solana", "1m-10m", "20260825", 1, 4, 1234);
  assert.equal(filtered[0].components[1].style, 1);
  assert.equal(filtered[2].components[3].style, 1);
  assert.deepEqual(filtered[3].components.map((button) => button.label), [
    "Prev", "Page 2 / 4", "Next", "Refresh prices",
  ]);
  assert.equal(filtered[3].components[1].disabled, true);

  const onePage = filterComponents("bsc", "all", "20260825", 0, 1, 1234);
  const onePageIds = onePage.flatMap((row) => row.components.map((button) => button.custom_id));
  assert.equal(onePageIds.length, new Set(onePageIds).size);
  assert.equal(onePage[3].components[0].disabled, true);
  assert.equal(onePage[3].components[2].disabled, true);
  assert.notEqual(onePage[3].components[0].custom_id, onePage[3].components[2].custom_id);

  const snapshot = {
    runnerUniverse: [
      safeRunner(),
      safeRunner({ symbol: "BNB", mint: "0xbnb", chain: "bsc", peakMarketCap: 2_000_000 }),
    ],
  };
  const counted = filterComponents("solana", "250k-500k", "20260825", 0, 1, 1234, snapshot);
  assert.equal(counted[0].components[0].label, "All (1)");
  assert.equal(counted[0].components[1].label, "Solana (1)");
  assert.equal(counted[0].components[2].label, "BNB (0)");
  assert.equal(counted[0].components[2].disabled, true);
  assert.equal(counted[2].components[3].label, "$1M-$10M (0)");
  assert.equal(counted[2].components[3].disabled, true);
});


test("Discord requires a fresh launch or a size-adjusted trailing-day move", () => {
  assert.equal(activityEligible(safeRunner({ ageHours: 12, change24h: -80 })), true);
  assert.equal(activityEligible(safeRunner({ ageHours: 12, scores: { runner: 39 } })), false);
  assert.equal(activityEligible(safeRunner({ ageHours: 72, change24h: 149 })), false);
  assert.equal(activityEligible(safeRunner({ ageHours: 72, change24h: 150 })), true);
  assert.equal(activityEligible(safeRunner({
    ageHours: 72,
    peakMarketCap: 750_000,
    change24h: 99,
  })), false);
  assert.equal(activityEligible(safeRunner({
    ageHours: 72,
    peakMarketCap: 750_000,
    change24h: 100,
  })), true);
  assert.equal(activityEligible(safeRunner({
    ageHours: 72,
    peakMarketCap: 2_000_000,
    change24h: 74,
  })), false);
  assert.equal(activityEligible(safeRunner({
    ageHours: 72,
    peakMarketCap: 2_000_000,
    change24h: 75,
  })), true);
  assert.equal(activityEligible(safeRunner({
    ageHours: 72,
    peakMarketCap: 12_000_000,
    change24h: 50,
  })), true);
  assert.equal(activityEligible(safeRunner({
    ageHours: 72,
    peakMarketCap: 25_000_000,
    change24h: 30,
  })), true);
});


test("Discord rejects confirmed danger without treating missing legacy data as a rug", () => {
  assert.equal(securityEligible(safeRunner()), true);
  assert.equal(securityEligible(safeRunner({ top10Pct: 31 })), false);
  assert.equal(securityEligible(safeRunner({ top10Pct: null })), false);
  assert.equal(securityEligible(safeRunner({ holders: null })), false);
  assert.equal(securityEligible(safeRunner({ holders: 0 })), false);
  assert.equal(securityEligible(safeRunner({
    mint: "0x02fca66c1d1afb4e2a7884261eb00f63598a7436",
    chain: "bsc",
    holders: null,
    top10Pct: null,
  })), true);
  assert.equal(securityEligible(safeRunner({
    mint: "0xb0f09ea9ae0515c3551080d4a745c8115aa30e37",
    chain: "bsc",
  })), false);
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


test("every chain and market-cap filter avoids Dexscreener on ordinary clicks", async () => {
  resetRefreshStateForTests();
  const originalFetch = globalThis.fetch;
  let dexCalls = 0;
  let snapshotCalls = 0;
  const chains = ["solana", "bsc", "base", "ethereum", "robinhood"];
  const peaks = [300_000, 700_000, 2_000_000, 12_000_000];
  const runnerUniverse = chains.flatMap((chain, chainIndex) => peaks.map((peak, bandIndex) =>
    safeRunner({
      symbol: `${chainIndex}${bandIndex}`,
      mint: `mint-${chainIndex}-${bandIndex}`,
      chain,
      marketCap: peak,
      peakMarketCap: peak,
    })));

  globalThis.fetch = async (url) => {
    if (String(url).includes("api.dexscreener.com")) {
      dexCalls += 1;
      throw new Error("ordinary filters must not request live prices");
    }
    snapshotCalls += 1;
    return new Response(JSON.stringify({
      generatedAt: "2026-08-25T00:00:00Z",
      runnerUniverse,
    }), { status: 200, headers: { "content-type": "application/json" } });
  };

  try {
    const request = new Request("https://app.test/api/discord-interactions");
    const interaction = { guild_id: "g", channel_id: "c", message: { id: "m" } };
    const actions = [
      ...chains.map((chain) => ({ chain, band: "all", source: "chain" })),
      ...["250k-500k", "500k-1m", "1m-10m", "10m-plus"].map((band) => ({
        chain: "all",
        band,
        source: "band",
      })),
    ];

    for (const action of actions) {
      const result = await renderFilterResponse(request, interaction, {
        ...action,
        date: "latest",
        page: 0,
        refreshedAt: 0,
        refresh: false,
      }, 1_000);
      assert.equal(result.error, undefined);
      assert.equal(result.embeds.length, 1);
      assert.equal(result.components.length, 4);
    }

    assert.equal(snapshotCalls, 1);
    assert.equal(dexCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
    resetRefreshStateForTests();
  }
});
