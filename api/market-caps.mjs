import { createHmac, timingSafeEqual } from "node:crypto";

const MAX_MINTS = 30;
const DEX_TIMEOUT_MS = 1000;
const SUPPORTED_CHAINS = new Set(["solana", "bsc", "base", "ethereum", "robinhood"]);

function proxySecret() {
  return String(process.env.MARKET_CAP_PROXY_SECRET || process.env.DISCORD_BOT_TOKEN || "");
}

function normalizeChain(value) {
  const chain = String(value || "").toLowerCase();
  if (chain === "bnb") return "bsc";
  if (chain === "eth") return "ethereum";
  if (chain === "sol") return "solana";
  return chain;
}

function validMint(chain, mint) {
  if (chain === "solana") return /^[1-9A-HJ-NP-Za-km-z]{32,48}$/.test(mint);
  return /^0x[0-9a-fA-F]{40}$/.test(mint);
}

export function canonicalMarketCapRequest(chainValue, mintValues) {
  const chain = normalizeChain(chainValue);
  if (!SUPPORTED_CHAINS.has(chain)) return null;
  const mints = [...new Set((mintValues || []).map((mint) => String(mint || "").trim()))]
    .filter((mint) => validMint(chain, mint))
    .sort((left, right) => left.localeCompare(right));
  if (!mints.length || mints.length > MAX_MINTS) return null;
  return { chain, mints, value: `${chain}:${mints.join(",")}` };
}

export function marketCapSignature(chain, mints, secret = proxySecret()) {
  const canonical = canonicalMarketCapRequest(chain, mints);
  if (!canonical || !secret) return "";
  return createHmac("sha256", secret).update(canonical.value).digest("hex");
}

export function buildMarketCapProxyUrl(origin, chain, mints, secret = proxySecret()) {
  const canonical = canonicalMarketCapRequest(chain, mints);
  if (!canonical || !secret) return "";
  const url = new URL("/api/market-caps", origin);
  url.searchParams.set("chain", canonical.chain);
  url.searchParams.set("mints", canonical.mints.join(","));
  url.searchParams.set("sig", marketCapSignature(canonical.chain, canonical.mints, secret));
  return url.toString();
}

function signaturesMatch(expected, provided) {
  if (!/^[0-9a-f]{64}$/i.test(expected) || !/^[0-9a-f]{64}$/i.test(provided || "")) {
    return false;
  }
  return timingSafeEqual(Buffer.from(expected, "hex"), Buffer.from(provided, "hex"));
}

function responseJson(body, status = 200, cached = false) {
  const headers = {
    "content-type": "application/json; charset=utf-8",
    "x-content-type-options": "nosniff",
  };
  if (cached) {
    // The browser does not retain this private service response. Vercel's CDN
    // does, and request collapsing turns a multi-instance refresh burst into
    // one upstream Dexscreener call per canonical token batch.
    headers["cache-control"] = "public, max-age=0, must-revalidate";
    headers["cdn-cache-control"] = "public, s-maxage=30, stale-while-revalidate=300";
    headers["vercel-cdn-cache-control"] = "public, s-maxage=30, stale-while-revalidate=300";
  } else {
    headers["cache-control"] = "private, no-store";
  }
  return new Response(JSON.stringify(body), { status, headers });
}

export async function handleMarketCaps(request, fetchImpl = fetch, secret = proxySecret()) {
  if (request.method !== "GET") return responseJson({ error: "method not allowed" }, 405);
  const url = new URL(request.url);
  const canonical = canonicalMarketCapRequest(
    url.searchParams.get("chain"),
    String(url.searchParams.get("mints") || "").split(","),
  );
  if (!canonical) return responseJson({ error: "invalid market-cap request" }, 400);
  const expected = marketCapSignature(canonical.chain, canonical.mints, secret);
  if (!signaturesMatch(expected, url.searchParams.get("sig"))) {
    return responseJson({ error: "unauthorized" }, 401);
  }

  const addresses = canonical.mints.map(encodeURIComponent).join(",");
  try {
    const upstream = await fetchImpl(
      `https://api.dexscreener.com/tokens/v1/${encodeURIComponent(canonical.chain)}/${addresses}`,
      { headers: { accept: "application/json" }, signal: AbortSignal.timeout(DEX_TIMEOUT_MS) },
    );
    if (!upstream.ok) return responseJson({ error: `Dexscreener HTTP ${upstream.status}` }, 502);
    const pairs = await upstream.json();
    const allowed = new Set(canonical.mints.map((mint) => mint.toLowerCase()));
    const exact = (Array.isArray(pairs) ? pairs : []).filter((pair) =>
      allowed.has(String(pair?.baseToken?.address || "").toLowerCase())
      && Number(pair?.marketCap) > 0);
    return responseJson(exact, 200, true);
  } catch (_) {
    return responseJson({ error: "market caps temporarily unavailable" }, 502);
  }
}

export default {
  fetch(request) {
    return handleMarketCaps(request);
  },
};
