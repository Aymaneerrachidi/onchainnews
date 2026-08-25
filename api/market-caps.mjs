import { createHash, createHmac, randomUUID, timingSafeEqual } from "node:crypto";

const MAX_MINTS = 30;
const DEX_TIMEOUT_MS = 1000;
const REDIS_TIMEOUT_MS = 500;
const FRESH_CAPS_MS = 30_000;
const STALE_CAPS_SECONDS = 330;
const LOCK_MS = 2_000;
const SUPPORTED_CHAINS = new Set(["solana", "bsc", "base", "ethereum", "robinhood"]);

function proxySecret() {
  return String(process.env.MARKET_CAP_PROXY_SECRET || process.env.DISCORD_BOT_TOKEN || "");
}

export function redisConfig(env = process.env) {
  const url = String(env.KV_REST_API_URL || env.UPSTASH_REDIS_REST_URL || "").replace(/\/$/, "");
  const token = String(env.KV_REST_API_TOKEN || env.UPSTASH_REDIS_REST_TOKEN || "");
  return url && token ? { url, token } : null;
}

async function redisCommand(config, command, fetchImpl = fetch) {
  if (!config) return null;
  const response = await fetchImpl(config.url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${config.token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(command),
    signal: AbortSignal.timeout(REDIS_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`Redis HTTP ${response.status}`);
  return (await response.json())?.result ?? null;
}

function distributedKeys(canonical) {
  const digest = createHash("sha256").update(canonical.value).digest("hex").slice(0, 32);
  return {
    cache: `onchain:mc:v1:${digest}`,
    lock: `onchain:mc:lock:v1:${digest}`,
  };
}

async function readDistributedCaps(config, key, fetchImpl) {
  try {
    const value = await redisCommand(config, ["GET", key], fetchImpl);
    if (!value) return null;
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    if (!Array.isArray(parsed?.pairs) || !Number.isFinite(Number(parsed?.fetchedAt))) return null;
    return parsed;
  } catch (_) {
    return null;
  }
}

async function acquireDistributedLock(config, key, owner, fetchImpl) {
  try {
    return await redisCommand(config, ["SET", key, owner, "NX", "PX", LOCK_MS], fetchImpl) === "OK";
  } catch (_) {
    return false;
  }
}

async function storeDistributedCaps(config, key, pairs, fetchedAt, fetchImpl) {
  try {
    await redisCommand(config, [
      "SET", key, JSON.stringify({ pairs, fetchedAt }), "EX", STALE_CAPS_SECONDS,
    ], fetchImpl);
  } catch (_) {
    // The live response still succeeds; CDN and in-instance caches remain.
  }
}

async function waitForDistributedCaps(config, key, fetchImpl) {
  // Cold followers wait briefly for the elected leader. Two bounded reads are
  // cheaper than allowing every Vercel instance to call Dexscreener, while
  // remaining comfortably inside Discord's three-second deadline.
  for (const delay of [120, 240]) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    const cached = await readDistributedCaps(config, key, fetchImpl);
    if (cached) return cached;
  }
  return null;
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

function responseJson(body, status = 200, cached = false, extraHeaders = {}) {
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
  Object.assign(headers, extraHeaders);
  return new Response(JSON.stringify(body), { status, headers });
}

export async function handleMarketCaps(
  request,
  fetchImpl = fetch,
  secret = proxySecret(),
  distributed = redisConfig(),
  redisFetchImpl = fetch,
) {
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

  const keys = distributedKeys(canonical);
  const cached = distributed
    ? await readDistributedCaps(distributed, keys.cache, redisFetchImpl)
    : null;
  const cacheAge = cached ? Date.now() - Number(cached.fetchedAt) : Number.POSITIVE_INFINITY;
  if (cached && cacheAge <= FRESH_CAPS_MS) {
    return responseJson(cached.pairs, 200, true, { "x-market-source": "redis-fresh" });
  }

  let ownsLock = true;
  if (distributed) {
    ownsLock = await acquireDistributedLock(distributed, keys.lock, randomUUID(), redisFetchImpl);
    if (!ownsLock && cached) {
      return responseJson(cached.pairs, 200, true, { "x-market-source": "redis-stale" });
    }
    if (!ownsLock) {
      const filled = await waitForDistributedCaps(distributed, keys.cache, redisFetchImpl);
      if (filled) {
        return responseJson(filled.pairs, 200, true, { "x-market-source": "redis-follower" });
      }
      return responseJson(
        { error: "global refresh already in progress" },
        409,
        false,
        { "retry-after": "1", "x-market-source": "locked" },
      );
    }
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
    if (distributed) {
      await storeDistributedCaps(distributed, keys.cache, exact, Date.now(), redisFetchImpl);
    }
    return responseJson(exact, 200, true, {
      "x-market-source": distributed ? "dex-lock-owner" : "dex",
    });
  } catch (_) {
    if (cached) {
      return responseJson(cached.pairs, 200, true, { "x-market-source": "redis-stale" });
    }
    return responseJson({ error: "market caps temporarily unavailable" }, 502);
  }
}

export default {
  fetch(request) {
    return handleMarketCaps(request);
  },
};
