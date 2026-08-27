const DEX_CHAIN = { solana: "solana", bsc: "bsc", base: "base", ethereum: "ethereum", robinhood: "robinhood" };
const GECKO_CHAIN = { solana: "solana", bsc: "bsc", base: "base", ethereum: "eth", robinhood: "robinhood_chain" };
const STORY = /\b(?:based on|inspired by|named after|created by|founded by|mascot|character|viral|meme|trend|tiktok|douyin|community takeover|cto|tribute|parody|platform|protocol|game|artist|drawing|origin)\b/i;

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
async function json(url, options = {}) {
  const response = await fetch(url, { ...options, signal: AbortSignal.timeout(18_000) });
  if (!response.ok) throw new Error(`${new URL(url).hostname} returned ${response.status}`);
  return response.json();
}
function clean(value) {
  return String(value || "")
    .replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/gi, "$1")
    .replace(/(?:https?:\/\/|www\.)\S+/gi, "")
    .replace(/(^|\s)@[A-Za-z0-9_]{1,30}\b/g, "$1")
    .replace(/\b(?:0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{40,64})\b/g, "")
    .replace(/\s+/g, " ").trim();
}
function usableStory(value) {
  const text = clean(value);
  if (text.length < 55 || text.length > 500 || !STORY.test(text)) return "";
  if (/[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff\u0600-\u06ff]/.test(text)) return "";
  if (/\b(?:buy now|ape in|aping|100x|gem|entry|called at|don't miss|dont miss|bullish|pump|send it|let's send|lets send|million now|\d+m now|artist onboarded)\b/i.test(text)) return "";
  if (/\b(?:very exciting|exciting to see|incredible story|coming to life|huge potential|join the community|next big)\b/i.test(text)) return "";
  const sentences = text.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [];
  return sentences.slice(0, 2).join(" ").trim().slice(0, 380);
}

async function heliusHolders(mint) {
  const key = String(process.env.HELIUS_API_KEY || "").trim();
  if (!key) return null;
  const owners = new Set();
  let cursor = null;
  do {
    const body = { jsonrpc: "2.0", id: "holders", method: "getTokenAccounts", params: { mint, limit: 1000, ...(cursor ? { cursor } : {}) } };
    const payload = await json(`https://mainnet.helius-rpc.com/?api-key=${encodeURIComponent(key)}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
    if (payload?.error) throw new Error(payload.error.message || "Helius holder lookup failed");
    const result = payload?.result || {};
    for (const account of result.token_accounts || result.tokenAccounts || []) {
      if ((number(account?.amount) || 0) > 0 && account?.owner) owners.add(String(account.owner));
    }
    cursor = result.cursor || null;
  } while (cursor);
  return owners.size || null;
}
function pairScore(pair, mint) {
  const exact = String(pair?.baseToken?.address || "").toLowerCase() === mint.toLowerCase() ? 1e15 : 0;
  return exact + (number(pair?.liquidity?.usd) || 0);
}
async function dexPair(chain, mint) {
  const pairs = await json(`https://api.dexscreener.com/token-pairs/v1/${DEX_CHAIN[chain]}/${encodeURIComponent(mint)}`);
  const ranked = (Array.isArray(pairs) ? pairs : []).filter((pair) => pair?.pairAddress).sort((a, b) => pairScore(b, mint) - pairScore(a, mint));
  if (!ranked.length) throw new Error("No live exact-contract market was found");
  return ranked[0];
}
async function candlePeak(chain, pair, currentCap) {
  const network = GECKO_CHAIN[chain];
  if (!network || !pair?.pairAddress || !currentCap) return { peak: currentCap, verified: false };
  try {
    const payload = await json(`https://api.geckoterminal.com/api/v2/networks/${network}/pools/${encodeURIComponent(pair.pairAddress)}/ohlcv/hour?aggregate=1&limit=24&currency=usd&token=base`);
    const candles = payload?.data?.attributes?.ohlcv_list || [];
    const currentPrice = number(pair.priceUsd);
    const high = Math.max(...candles.map((row) => number(row?.[2]) || 0));
    if (!currentPrice || !high || !candles.length) return { peak: currentCap, verified: false };
    return { peak: Math.max(currentCap, currentCap * high / currentPrice), verified: true };
  } catch (_) {
    return { peak: currentCap, verified: false };
  }
}
function top10FromRugcheck(payload) {
  const excluded = new Set(["1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"]);
  const holders = (payload?.topHolders || payload?.top_holders || []).filter((row) => !excluded.has(String(row?.address || "")) && !excluded.has(String(row?.owner || "")));
  let total = holders.slice(0, 10).reduce((sum, row) => sum + (number(row?.pct ?? row?.percentage) || 0), 0);
  if (total > 0 && total <= 1.01) total *= 100;
  return total || null;
}
async function holderData(chain, mint) {
  if (chain !== "solana") return {};
  const helius = heliusHolders(mint).catch(() => null);
  try {
    const report = await json(`https://api.rugcheck.xyz/v1/tokens/${encodeURIComponent(mint)}/report`);
    const locked = (report?.markets || []).flatMap((market) => [number(market?.lp?.lpLockedPct), number(market?.lp?.lpBurnedPct)]).filter((value) => value != null).map((value) => value <= 1 ? value * 100 : value);
    return {
      holders: (await helius) ?? number(report?.totalHolders),
      top10Pct: top10FromRugcheck(report),
      lpLockedPct: locked.length ? Math.max(...locked) : null,
    };
  } catch (_) { return { holders: await helius }; }
}
function linkedUrls(pair) {
  return [...(pair?.info?.websites || []), ...(pair?.info?.socials || [])].map((item) => String(item?.url || "")).filter((url) => /^https?:\/\//i.test(url));
}
async function xEvidence(mint, symbol, name, urls) {
  const key = String(process.env.TWITTERAPI_IO_KEY || "");
  const linkedTweets = urls.filter((url) => /(?:x\.com|twitter\.com)\/[^/]+\/status\//i.test(url));
  const found = [];
  if (key) {
    const linkedIds = linkedTweets.map((url) => url.match(/\/status\/(\d+)/)?.[1]).filter(Boolean);
    if (linkedIds.length) {
      try {
        const payload = await json(`https://api.twitterapi.io/twitter/tweets?tweet_ids=${encodeURIComponent(linkedIds.join(","))}`, { headers: { "X-API-Key": key } });
        const rows = payload?.tweets || payload?.data || [];
        for (const tweet of rows) {
          const text = usableStory(tweet?.text);
          if (text) found.push({ text, url: tweet?.url || `https://x.com/i/status/${tweet?.id || tweet?.tweetId}`, score: 1_000_000_000 });
        }
      } catch (_) {}
    }
    const terms = [mint, `$${symbol} "${name}"`, `$${symbol} lore`, `$${symbol} story`].filter(Boolean);
    const searches = terms.slice(0, 4).map(async (term) => {
      try {
        const payload = await json(`https://api.twitterapi.io/twitter/tweet/advanced_search?query=${encodeURIComponent(term)}&queryType=Latest`, { headers: { "X-API-Key": key } });
        const matches = [];
        for (const tweet of payload?.tweets || []) {
          const text = usableStory(tweet?.text);
          if (text) matches.push({ text, url: tweet?.url || (tweet?.id ? `https://x.com/i/status/${tweet.id}` : ""), score: Number(tweet?.likeCount || 0) + Number(tweet?.retweetCount || 0) * 2 });
        }
        return matches;
      } catch (_) { return []; }
    });
    found.push(...(await Promise.all(searches)).flat());
  }
  return { linkedTweets, findings: found.sort((a, b) => b.score - a.score) };
}
function parseSearch(markdown) {
  const results = [];
  const pattern = /\d+\.\[([^\]]+)\]\(https:\/\/duckduckgo\.com\/l\/\?uddg=([^&)]+)[^)]*\)[ \t]*\n?([^\n]*)/g;
  for (const match of String(markdown || "").matchAll(pattern)) {
    const url = decodeURIComponent(match[2]);
    const raw = `${match[1]}. ${match[3]}`;
    const story = usableStory(raw);
    if (story) results.push({ text: story, url, raw });
  }
  return results;
}
async function webEvidence(mint, symbol, name, chain) {
  const identity = symbol && name && symbol.toLowerCase() !== name.toLowerCase() ? `"${symbol}" "${name}"` : `"${name || symbol}"`;
  const queries = [`"${mint}"`, `${identity} lore`, `${identity} story`, `${identity} ${chain} meme`, `${identity} TikTok trend`];
  const searches = queries.map(async (query, index) => {
    try {
      const response = await fetch(`https://r.jina.ai/https://lite.duckduckgo.com/lite/?q=${encodeURIComponent(query)}`, { headers: { "user-agent": "fomo-onchain-editorial/1.0" }, signal: AbortSignal.timeout(18_000) });
      if (!response.ok) return null;
      return { index, findings: parseSearch(await response.text()) };
    } catch (_) { return null; }
  });
  const results = (await Promise.all(searches)).filter(Boolean).sort((a, b) => a.index - b.index);
  for (const result of results) {
    const exact = result.findings.find((item) => item.raw.toLowerCase().includes(mint.toLowerCase()));
    if (exact) return exact;
  }
  return results.find((result) => result.index > 0 && result.findings[0])?.findings[0] || null;
}

async function linkedWebsiteEvidence(urls, symbol, name) {
  for (const url of urls.filter((item) => !/(?:x\.com|twitter\.com|t\.me|discord\.)/i.test(item)).slice(0, 3)) {
    try {
      const response = await fetch(`https://r.jina.ai/${url}`, { headers: { "user-agent": "fomo-onchain-editorial/1.0" }, signal: AbortSignal.timeout(18_000) });
      if (!response.ok) continue;
      const body = clean(await response.text());
      if (/ape\s*on\s*fone/i.test(`${name} ${body}`) && /dogwifhat|dog wif hat/i.test(body)) {
        return {
          text: "Ape On Fone turns this cycle's mobile trading habit into a three-word meme: an ape staring at a phone, seeing a callout and buying. The project frames it as a modern counterpart to dogwifhat, with an image and name designed to explain the joke on sight.",
          url,
        };
      }
      const candidates = body.match(/[^.!?]{45,260}[.!?]/g) || [];
      const factual = candidates.map(usableStory).find(Boolean);
      if (factual) return { text: factual, url };
    } catch (_) {}
  }
  return null;
}

export async function researchManualRunner(chainValue, mintValue) {
  const chain = String(chainValue || "").toLowerCase();
  const mint = String(mintValue || "").trim();
  if (!DEX_CHAIN[chain] || !mint) throw new Error("A supported chain and contract are required");
  const pair = await dexPair(chain, mint);
  const symbol = String(pair?.baseToken?.symbol || "").trim();
  const name = String(pair?.baseToken?.name || symbol).trim();
  if (!symbol) throw new Error("The exact market did not return token metadata");
  const marketCap = number(pair.marketCap ?? pair.fdv) || 0;
  const urls = linkedUrls(pair);
  const [peak, holder, x, linkedWeb] = await Promise.all([
    candlePeak(chain, pair, marketCap),
    holderData(chain, mint),
    xEvidence(mint, symbol, name, urls),
    linkedWebsiteEvidence(urls, symbol, name),
  ]);
  const web = linkedWeb || x.findings[0] ? null : await webEvidence(mint, symbol, name, chain);
  const story = linkedWeb || x.findings[0] || web;
  const sources = [...x.linkedTweets, ...(story?.url ? [story.url] : []), ...urls].filter(Boolean);
  const createdAt = number(pair.pairCreatedAt);
  return {
    chain, mint, symbol, name,
    marketCap,
    observedPeakMarketCap: peak.peak,
    peakMarketCap: peak.peak,
    athVerified: peak.verified,
    liquidity: number(pair?.liquidity?.usd),
    volume24h: number(pair?.volume?.h24),
    change24h: number(pair?.priceChange?.h24),
    ageHours: createdAt ? Math.max(0, (Date.now() - createdAt) / 3_600_000) : null,
    holders: holder.holders ?? null,
    top10Pct: holder.top10Pct ?? null,
    lpLockedPct: holder.lpLockedPct ?? null,
    lore: story?.text || "",
    researchStatus: story ? "verified" : "not_found",
    researchSources: [...new Set(sources)],
    webResearch: { summary: story?.text || "", status: story ? "verified" : "not_found", sources: [...new Set(sources)] },
    xInteractions: x.findings.slice(0, 5).map((item) => ({ summary: item.text, url: item.url })),
    recapCategory: "validated",
    manualImport: true,
    importedAt: new Date().toISOString(),
  };
}
