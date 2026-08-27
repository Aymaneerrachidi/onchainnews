import test from "node:test";
import assert from "node:assert/strict";

import { researchManualRunner } from "../api/manual-runner.mjs";

test("manual runner import hydrates one exact contract and researches its story", async () => {
  const originalFetch = globalThis.fetch;
  const originalKey = process.env.TWITTERAPI_IO_KEY;
  process.env.TWITTERAPI_IO_KEY = "test-key";
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("api.dexscreener.com")) return Response.json([{
      pairAddress: "pool-1",
      baseToken: { address: "mint-1", symbol: "CAT", name: "Internet Cat" },
      priceUsd: "1",
      marketCap: 1_000_000,
      liquidity: { usd: 100_000 },
      volume: { h24: 2_000_000 },
      priceChange: { h24: 25 },
      pairCreatedAt: Date.now() - 3_600_000,
      info: { socials: [{ url: "https://x.com/cat/status/123" }] },
    }]);
    if (url.includes("api.geckoterminal.com")) return Response.json({ data: { attributes: { ohlcv_list: [[Date.now(), 1, 2, 0.5, 1.5, 10]] } } });
    if (url.includes("api.rugcheck.xyz")) return Response.json({ totalHolders: 1200, topHolders: Array.from({ length: 10 }, (_, index) => ({ address: `holder-${index}`, pct: 1 })) });
    if (url.includes("api.twitterapi.io")) return Response.json({ tweets: [{ id: "456", text: "Internet Cat is a community meme inspired by a rescued shelter cat whose adoption video went viral.", likeCount: 40, retweetCount: 5 }] });
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    const runner = await researchManualRunner("solana", "mint-1");
    assert.equal(runner.symbol, "CAT");
    assert.equal(runner.marketCap, 1_000_000);
    assert.equal(runner.observedPeakMarketCap, 2_000_000);
    assert.equal(runner.athVerified, true);
    assert.equal(runner.holders, 1200);
    assert.equal(runner.top10Pct, 10);
    assert.match(runner.lore, /rescued shelter cat/);
    assert.equal(runner.manualImport, true);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalKey == null) delete process.env.TWITTERAPI_IO_KEY;
    else process.env.TWITTERAPI_IO_KEY = originalKey;
  }
});
