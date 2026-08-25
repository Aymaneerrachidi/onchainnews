import assert from "node:assert/strict";
import test from "node:test";

import { sharedMarketFetch } from "../api/discord-interactions.mjs";
import {
  buildMarketCapProxyUrl,
  handleMarketCaps,
  marketCapSignature,
} from "../api/market-caps.mjs";

const SECRET = "test-only-proxy-secret";
const MINT_A = "0x1111111111111111111111111111111111111111";
const MINT_B = "0x2222222222222222222222222222222222222222";

test("market-cap proxy URLs are signed and canonical across token order", () => {
  const first = buildMarketCapProxyUrl("https://app.test/path", "bnb", [MINT_B, MINT_A], SECRET);
  const second = buildMarketCapProxyUrl("https://app.test/other", "bsc", [MINT_A, MINT_B], SECRET);

  assert.equal(first, second);
  const url = new URL(first);
  assert.equal(url.pathname, "/api/market-caps");
  assert.equal(url.searchParams.get("chain"), "bsc");
  assert.equal(url.searchParams.get("mints"), `${MINT_A},${MINT_B}`);
  assert.equal(url.searchParams.get("sig"), marketCapSignature("bsc", [MINT_A, MINT_B], SECRET));
});

test("market-cap proxy rejects unsigned and invalid token requests", async () => {
  const unsigned = await handleMarketCaps(
    new Request(`https://app.test/api/market-caps?chain=bsc&mints=${MINT_A}`),
    fetch,
    SECRET,
  );
  const invalid = await handleMarketCaps(
    new Request("https://app.test/api/market-caps?chain=bsc&mints=not-a-contract&sig=x"),
    fetch,
    SECRET,
  );

  assert.equal(unsigned.status, 401);
  assert.equal(invalid.status, 400);
  assert.equal(unsigned.headers.get("cache-control"), "private, no-store");
});

test("market-cap proxy returns exact pairs with shared CDN cache headers", async () => {
  const url = buildMarketCapProxyUrl("https://app.test", "bsc", [MINT_A], SECRET);
  let calls = 0;
  const fetchStub = async (upstream) => {
    calls += 1;
    assert.match(String(upstream), new RegExp(`/tokens/v1/bsc/${MINT_A}$`));
    return new Response(JSON.stringify([
      { baseToken: { address: MINT_A }, marketCap: 2_000_000, liquidity: { usd: 100_000 } },
      { baseToken: { address: MINT_B }, marketCap: 99_000_000, liquidity: { usd: 5_000_000 } },
    ]), { status: 200, headers: { "content-type": "application/json" } });
  };

  const response = await handleMarketCaps(new Request(url), fetchStub, SECRET);
  const pairs = await response.json();

  assert.equal(response.status, 200);
  assert.equal(calls, 1);
  assert.equal(pairs.length, 1);
  assert.equal(pairs[0].baseToken.address, MINT_A);
  assert.match(response.headers.get("vercel-cdn-cache-control"), /s-maxage=30/);
  assert.match(response.headers.get("vercel-cdn-cache-control"), /stale-while-revalidate=300/);
});

test("Discord rewrites Dexscreener refreshes through the signed project proxy", async () => {
  const previous = process.env.MARKET_CAP_PROXY_SECRET;
  process.env.MARKET_CAP_PROXY_SECRET = SECRET;
  let requested = "";
  const fetchStub = async (url) => {
    requested = String(url);
    return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
  };

  try {
    const proxied = sharedMarketFetch("https://app.test/api/discord-interactions", fetchStub);
    await proxied(`https://api.dexscreener.com/tokens/v1/bsc/${MINT_A}`, {});
    const url = new URL(requested);
    assert.equal(url.origin, "https://app.test");
    assert.equal(url.pathname, "/api/market-caps");
    assert.equal(url.searchParams.get("chain"), "bsc");
    assert.equal(url.searchParams.get("sig"), marketCapSignature("bsc", [MINT_A], SECRET));
  } finally {
    if (previous === undefined) delete process.env.MARKET_CAP_PROXY_SECRET;
    else process.env.MARKET_CAP_PROXY_SECRET = previous;
  }
});
