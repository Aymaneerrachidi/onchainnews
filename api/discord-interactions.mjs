import { createPublicKey, verify } from "node:crypto";

const DISCORD_PUBLIC_KEY_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const REFRESH_PREFIX = "refresh_mc:";
const FILTER_PREFIX = "rfilter:";
const FILTER_REFRESH_PREFIX = "rrefresh:";
const COOLDOWN_SECONDS = 30;
const MARKET_CACHE_SECONDS = 30;
const SNAPSHOT_CACHE_SECONDS = 30;
const MAX_CONTRACTS = 100;
const PAGE_SIZE = 8;
const DEX_TIMEOUT_MS = 1800;
const SNAPSHOT_TIMEOUT_MS = 1800;
const HOLDER_STRUCTURE_EXCEPTIONS = new Set([
  "bsc:0x02fca66c1d1afb4e2a7884261eb00f63598a7436", // NVDAB
  "bsc:0xcaae2a2f939f51d97cdfa9a86e79e3f085b799f3", // TUT
]);
const ACTIVITY_EXCEPTIONS = new Set(HOLDER_STRUCTURE_EXCEPTIONS);
const MANUALLY_EXCLUDED_CONTRACTS = new Set([
  "bsc:0xb0f09ea9ae0515c3551080d4a745c8115aa30e37", // DOS
]);
const FOMO_TOKEN = /https:\/\/fomo\.family\/tokens\/([^/\s)>]+)\/([^\s)>]+)/g;

// Warm Vercel instances retain these maps between requests. Dex results and
// the runner snapshot are shared by all readers for 30 seconds, while refresh
// locks stop one Discord message from creating a click burst.
const marketCache = new Map();
const messageRefreshes = new Map();
const messageLocks = new Map();
let snapshotCache = null;

const DEX_CHAIN_ALIASES = new Map([
  ["bnb", "bsc"],
  ["eth", "ethereum"],
  ["sol", "solana"],
]);

const CHAINS = [
  ["All chains", "all"],
  ["Solana", "solana"],
  ["BNB", "bsc"],
  ["Base", "base"],
  ["Ethereum", "ethereum"],
  ["Robinhood", "robinhood"],
];

const BANDS = [
  ["All MC", "all"],
  ["$250K-$500K", "250k-500k"],
  ["$500K-$1M", "500k-1m"],
  ["$1M-$10M", "1m-10m"],
  ["$10M+", "10m-plus"],
];

const CHAIN_LABELS = new Map(CHAINS.map(([label, value]) => [value, label]));
const BAND_LABELS = new Map(BANDS.map(([label, value]) => [value, label]));

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export function verifyDiscordRequest(publicKeyHex, signatureHex, timestamp, rawBody) {
  try {
    if (!/^[0-9a-f]{64}$/i.test(publicKeyHex || "")) return false;
    if (!/^[0-9a-f]{128}$/i.test(signatureHex || "")) return false;
    const key = createPublicKey({
      key: Buffer.concat([DISCORD_PUBLIC_KEY_PREFIX, Buffer.from(publicKeyHex, "hex")]),
      format: "der",
      type: "spki",
    });
    return verify(
      null,
      Buffer.from(`${timestamp}${rawBody}`, "utf8"),
      key,
      Buffer.from(signatureHex, "hex"),
    );
  } catch (_) {
    return false;
  }
}

function money(value) {
  const number = Number(value) || 0;
  const absolute = Math.abs(number);
  if (absolute >= 1e9) return `$${(number / 1e9).toFixed(1)}B`;
  if (absolute >= 1e6) return `$${(number / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `$${Math.round(number / 1e3)}K`;
  return `$${Math.round(number)}`;
}

function count(value) {
  return Math.max(0, Number(value) || 0).toLocaleString("en-US", {
    maximumFractionDigits: 0,
  });
}

function reportDateKey(value) {
  const match = String(value || "").match(/^(\d{4})-?(\d{2})-?(\d{2})/);
  return match ? `${match[1]}${match[2]}${match[3]}` : "latest";
}

function componentButton(label, customId, active = false, disabled = false) {
  return {
    type: 2,
    style: active ? 1 : 2,
    label,
    custom_id: customId,
    ...(disabled ? { disabled: true } : {}),
  };
}

function publicComponents(epochSeconds = 0, reportDate = "latest") {
  const date = reportDateKey(reportDate);
  return [
    {
      type: 1,
      components: CHAINS.slice(0, 5).map(([label, value]) =>
        componentButton(label, `${FILTER_PREFIX}${value}:all:${date}:0:chain`)),
    },
    {
      type: 1,
      components: CHAINS.slice(5).map(([label, value]) =>
        componentButton(label, `${FILTER_PREFIX}${value}:all:${date}:0:chain`)),
    },
    {
      type: 1,
      components: BANDS.map(([label, value]) =>
        componentButton(label, `${FILTER_PREFIX}all:${value}:${date}:0:band`)),
    },
    {
      type: 1,
      components: [componentButton(
        "Refresh live MC",
        `${REFRESH_PREFIX}${Math.max(0, Number(epochSeconds) || 0)}:${date}`,
      )],
    },
  ];
}

function filterComponents(chain, band, date, page, pages, refreshedAt) {
  const filterId = (nextChain, nextBand, nextPage = 0, source = "nav") =>
    `${FILTER_PREFIX}${nextChain}:${nextBand}:${date}:${nextPage}:${source}`;
  const navigation = [];
  if (pages > 1) {
    navigation.push(componentButton("Previous", filterId(chain, band, Math.max(0, page - 1)), false, page <= 0));
    navigation.push(componentButton("Next", filterId(chain, band, Math.min(pages - 1, page + 1)), false, page >= pages - 1));
  }
  navigation.push(componentButton(
    "Refresh live MC",
    `${FILTER_REFRESH_PREFIX}${chain}:${band}:${date}:${page}:${refreshedAt}`,
  ));
  return [
    {
      type: 1,
      components: CHAINS.slice(0, 5).map(([label, value]) =>
        componentButton(label, filterId(value, band, 0, "chain"), value === chain)),
    },
    {
      type: 1,
      components: CHAINS.slice(5).map(([label, value]) =>
        componentButton(label, filterId(value, band, 0, "chain"), value === chain)),
    },
    {
      type: 1,
      components: BANDS.map(([label, value]) =>
        componentButton(label, filterId(chain, value, 0, "band"), value === band)),
    },
    { type: 1, components: navigation },
  ];
}

export function contractsFromEmbeds(embeds = []) {
  const contracts = new Map();
  const inspect = (value) => {
    for (const match of String(value || "").matchAll(FOMO_TOKEN)) {
      const chain = decodeURIComponent(match[1]).toLowerCase();
      const mint = decodeURIComponent(match[2]);
      contracts.set(`${chain}:${mint.toLowerCase()}`, { chain, mint, url: match[0] });
    }
  };
  for (const embed of embeds) {
    inspect(embed.title);
    inspect(embed.description);
    for (const field of embed.fields || []) {
      inspect(field.name);
      inspect(field.value);
    }
  }
  return [...contracts.values()].slice(0, MAX_CONTRACTS);
}

export function dexChain(chain) {
  const normalized = String(chain || "").trim().toLowerCase();
  return DEX_CHAIN_ALIASES.get(normalized) || normalized;
}

function replaceLineCap(line, contract, current) {
  const urlAt = line.indexOf(contract.url);
  if (urlAt < 0) return line;
  const boldStart = line.indexOf("**", urlAt + contract.url.length);
  if (boldStart < 0) return line;
  const boldEnd = line.indexOf("**", boldStart + 2);
  if (boldEnd < 0) return line;
  const result = line.slice(boldStart + 2, boldEnd);
  const live = `· now ${money(current)}`;
  const marker = /\s*(?:·|Â·|Ã‚Â·)\s*now\s+(?:\$[\d.,]+[KMB]?|n\/a|unavailable)/i;
  const updated = marker.test(result)
    ? result.replace(marker, ` ${live}`)
    : `${result} ${live}`;
  return `${line.slice(0, boldStart + 2)}${updated}${line.slice(boldEnd)}`;
}

export function applyLiveCaps(text, contracts, prices) {
  return String(text || "").split("\n").map((line) => {
    let updated = line;
    for (const contract of contracts) {
      const current = prices.get(`${contract.chain}:${contract.mint.toLowerCase()}`);
      if (current) updated = replaceLineCap(updated, contract, current);
    }
    return updated;
  }).join("\n");
}

function editableEmbed(embed) {
  const allowed = [
    "title", "description", "url", "timestamp", "color", "footer",
    "image", "thumbnail", "author", "fields",
  ];
  return Object.fromEntries(
    allowed.filter((key) => embed?.[key] !== undefined).map((key) => [key, embed[key]]),
  );
}

function cacheKey(contract) {
  return `${contract.chain}:${contract.mint.toLowerCase()}`;
}

export async function fetchLiveCaps(contracts, fetchImpl = fetch, nowMs = Date.now()) {
  const grouped = new Map();
  const prices = new Map();
  for (const contract of contracts) {
    const key = cacheKey(contract);
    const cached = marketCache.get(key);
    if (cached && cached.expiresAt > nowMs) {
      prices.set(key, cached.marketCap);
      continue;
    }
    const chain = dexChain(contract.chain);
    if (!chain) continue;
    if (!grouped.has(chain)) grouped.set(chain, []);
    grouped.get(chain).push(contract);
  }
  await Promise.all([...grouped.entries()].flatMap(([chain, rows]) => {
    const jobs = [];
    for (let start = 0; start < rows.length; start += 30) {
      const batch = rows.slice(start, start + 30);
      const addresses = batch.map((row) => encodeURIComponent(row.mint)).join(",");
      jobs.push(fetchImpl(`https://api.dexscreener.com/tokens/v1/${encodeURIComponent(chain)}/${addresses}`, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(DEX_TIMEOUT_MS),
      }).then((response) => response.ok ? response.json() : [])
        .then((pairs) => {
          for (const contract of batch) {
            const matches = (Array.isArray(pairs) ? pairs : []).filter((pair) =>
              String(pair?.baseToken?.address || "").toLowerCase() === contract.mint.toLowerCase()
              && Number(pair?.marketCap) > 0
            ).sort((a, b) => Number(b?.liquidity?.usd || 0) - Number(a?.liquidity?.usd || 0));
            if (matches.length) {
              const key = cacheKey(contract);
              const marketCap = Number(matches[0].marketCap);
              prices.set(key, marketCap);
              marketCache.set(key, {
                marketCap,
                expiresAt: nowMs + MARKET_CACHE_SECONDS * 1000,
              });
            }
          }
        }).catch(() => null));
    }
    return jobs;
  }));
  return prices;
}

export async function fetchRunnerSnapshot(url, fetchImpl = fetch, nowMs = Date.now()) {
  if (snapshotCache && snapshotCache.url === url && snapshotCache.expiresAt > nowMs) {
    return snapshotCache.payload;
  }
  const response = await fetchImpl(url, {
    headers: { accept: "application/json" },
    cache: "no-store",
    signal: AbortSignal.timeout(SNAPSHOT_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`runner snapshot HTTP ${response.status}`);
  const payload = await response.json();
  if (!payload || typeof payload !== "object") throw new Error("invalid runner snapshot");
  snapshotCache = {
    url,
    payload,
    expiresAt: nowMs + SNAPSHOT_CACHE_SECONDS * 1000,
  };
  return payload;
}

function runnerPeak(row) {
  return Math.max(
    Number(row?.peakMarketCap) || 0,
    Number(row?.observedPeakMarketCap) || 0,
    Number(row?.marketCap) || 0,
  );
}

function gmgnEvidence(row) {
  return row?.providerEvidence?.gmgn || {};
}

function contractKey(row) {
  return `${String(row?.chain || "").toLowerCase()}:${String(row?.mint || "").toLowerCase()}`;
}

function runnerWindowMovePct(row) {
  const gmgn = gmgnEvidence(row);
  const moves = [];
  for (const value of [row?.change24h, gmgn?.kline24hPeakFromOpenPct]) {
    const measured = Number(value);
    if (Number.isFinite(measured)) moves.push(measured);
  }
  const start = Number(row?.startMarketCap);
  const peak = runnerPeak(row);
  if (Number.isFinite(start) && start > 0 && peak > 0) {
    moves.push(((peak / start) - 1) * 100);
  }
  return moves.length ? Math.max(...moves) : Number.NEGATIVE_INFINITY;
}

export function activityEligible(row) {
  if (ACTIVITY_EXCEPTIONS.has(contractKey(row))) return true;
  const runnerScore = Number(row?.scores?.runner);
  if (!Number.isFinite(runnerScore) || runnerScore < 40) return false;
  const age = Number(row?.ageHours);
  if (Number.isFinite(age) && age >= 0 && age <= 24) return true;
  const peak = runnerPeak(row);
  let required = 150;
  if (peak >= 20_000_000) required = 30;
  else if (peak >= 10_000_000) required = 50;
  else if (peak >= 1_000_000) required = 75;
  else if (peak >= 500_000) required = 100;
  return runnerWindowMovePct(row) >= required;
}

export function securityEligible(row) {
  const gmgn = gmgnEvidence(row);
  const peak = runnerPeak(row);
  const chain = String(row?.chain || "").toLowerCase();
  const mint = String(row?.mint || "").toLowerCase();
  const key = `${chain}:${mint}`;
  const holderStructureException = HOLDER_STRUCTURE_EXCEPTIONS.has(key);
  const holdersKnown = row?.holders !== null && row?.holders !== undefined;
  const holders = Number(row?.holders);
  const top10Known = row?.top10Pct !== null && row?.top10Pct !== undefined;
  const top10 = Number(row?.top10Pct);
  const lpKnown = row?.lpLockedPct !== null && row?.lpLockedPct !== undefined;
  const lpLocked = Number(row?.lpLockedPct);
  const burned = String(gmgn?.burnStatus || "").toLowerCase() === "yes"
    || Number(gmgn?.burnRatio || 0) >= 0.90;
  const honeypot = gmgn?.isHoneypot === true
    || Number(gmgn?.isHoneypot) === 1
    || ["true", "yes"].includes(String(gmgn?.isHoneypot || "").toLowerCase());
  const devHoldKnown = gmgn?.devTeamHoldRate !== null
    && gmgn?.devTeamHoldRate !== undefined;
  const devHold = Number(gmgn?.devTeamHoldRate);
  const liquidityFloors = {
    solana: 40000,
    bsc: 30000,
    base: 30000,
    ethereum: 50000,
    robinhood: 30000,
  };
  return Boolean(
    row?.mint
    && !MANUALLY_EXCLUDED_CONTRACTS.has(key)
    && peak >= 250000
    && row?.rugged !== true
    && !honeypot
    && row?.mintAuthorityRenounced !== false
    && row?.freezeAuthorityDisabled !== false
    && (
      holderStructureException
      || (
        holdersKnown
        && Number.isFinite(holders)
        && holders > 0
        && top10Known
        && Number.isFinite(top10)
      )
    )
    && (!top10Known || top10 <= 30)
    && (!devHoldKnown || (Number.isFinite(devHold) && devHold <= 0.15))
    && (!lpKnown || !Number.isFinite(lpLocked) || lpLocked > 0 || burned)
    && Number(row?.liquidity || 0) >= Number(liquidityFloors[chain] || 30000)
    && gmgn?.washTrading !== true
  );
}

function inBand(peak, band) {
  if (band === "250k-500k") return peak >= 250000 && peak < 500000;
  if (band === "500k-1m") return peak >= 500000 && peak < 1000000;
  if (band === "1m-10m") return peak >= 1000000 && peak < 10000000;
  if (band === "10m-plus") return peak >= 10000000;
  return peak >= 250000;
}

export function filterRunnerRows(snapshot, chain = "all", band = "all") {
  const source = Array.isArray(snapshot?.runnerUniverse)
    ? snapshot.runnerUniverse
    : (Array.isArray(snapshot?.runners) ? snapshot.runners : []);
  const unique = new Map();
  for (const row of source) {
    const rowChain = String(row?.chain || "").toLowerCase();
    const key = `${rowChain}:${String(row?.mint || "").toLowerCase()}`;
    if (!securityEligible(row) || !activityEligible(row)) continue;
    if (chain !== "all" && rowChain !== chain) continue;
    if (!inBand(runnerPeak(row), band)) continue;
    const existing = unique.get(key);
    if (!existing || runnerPeak(row) > runnerPeak(existing)) unique.set(key, row);
  }
  return [...unique.values()].sort((left, right) =>
    runnerPeak(right) - runnerPeak(left)
    || Number(right?.volume24h || 0) - Number(left?.volume24h || 0));
}

function fomoUrl(row) {
  return `https://fomo.family/tokens/${encodeURIComponent(row.chain)}/${encodeURIComponent(row.mint)}`;
}

function shortCause(row) {
  const stated = String(row?.providerEvidence?.why?.cause || row?.catalyst || row?.lore || "").trim();
  if (!stated) return "";
  return stated.length > 105 ? `${stated.slice(0, 102).trimEnd()}…` : stated;
}

function filteredEmbed(rows, prices, chain, band, page, pages, total, refreshedAt) {
  const chainLabel = CHAIN_LABELS.get(chain) || chain;
  const bandLabel = BAND_LABELS.get(band) || band;
  const lines = rows.map((row) => {
    const key = `${String(row.chain).toLowerCase()}:${String(row.mint).toLowerCase()}`;
    const current = prices.get(key) || Number(row.marketCap) || 0;
    const holderText = row?.holders === null || row?.holders === undefined
      ? "holders unknown"
      : `${count(row.holders)} holders`;
    const top10Text = row?.top10Pct === null || row?.top10Pct === undefined
      ? "top10 unknown"
      : `top10 ${Number(row.top10Pct).toFixed(1)}%`;
    const stats = [
      `**now ${money(current)}**`,
      `24h high ${money(runnerPeak(row))}`,
      `liq ${money(row.liquidity)}`,
      holderText,
      top10Text,
    ].join(" · ");
    const cause = shortCause(row);
    return `[**$${String(row.symbol || "?").toUpperCase()}**](${fomoUrl(row)}) — ${stats}`
      + (cause ? `\n${cause}` : "");
  });
  return {
    color: 0x516AF6,
    title: `${chainLabel} · ${bandLabel} 24h peak`,
    description: lines.join("\n\n") || "No screened runners matched both filters.",
    footer: {
      text: `${total} screened runner${total === 1 ? "" : "s"} · page ${page + 1}/${pages} · MC refreshed ${new Date(refreshedAt * 1000).toISOString().slice(11, 19)} UTC`,
    },
  };
}

function parseFilterAction(customId) {
  const refresh = customId.startsWith(FILTER_REFRESH_PREFIX);
  const prefix = refresh ? FILTER_REFRESH_PREFIX : FILTER_PREFIX;
  if (!customId.startsWith(prefix)) return null;
  const parts = customId.slice(prefix.length).split(":");
  if (parts.length < 4) return null;
  const [chain, band, date, rawPage, rawRefresh = "0"] = parts;
  if (!CHAIN_LABELS.has(chain) || !BAND_LABELS.has(band)) return null;
  return {
    chain,
    band,
    date: reportDateKey(date),
    page: Math.max(0, Number.parseInt(rawPage, 10) || 0),
    refreshedAt: Math.max(0, Number.parseInt(rawRefresh, 10) || 0),
    refresh,
  };
}

function pruneRefreshState(now) {
  for (const [key, refreshedAt] of messageRefreshes) {
    if (now - refreshedAt > COOLDOWN_SECONDS * 4) messageRefreshes.delete(key);
  }
}

function messageKey(interaction) {
  return [
    String(interaction?.guild_id || "dm"),
    String(interaction?.channel_id || "unknown"),
    String(interaction?.message?.id || "unknown"),
  ].join(":");
}

async function pricesForMessage(key, contracts) {
  const existing = messageLocks.get(key);
  if (existing) return existing;
  const request = fetchLiveCaps(contracts).finally(() => messageLocks.delete(key));
  messageLocks.set(key, request);
  return request;
}

function snapshotUrl(request) {
  return String(process.env.RUNNER_SNAPSHOT_URL || new URL("/data/latest.json", request.url));
}

function repositorySnapshotUrl(date) {
  return `https://raw.githubusercontent.com/Aymaneerrachidi/onchainnews/main/web/data/latest.json?report=${encodeURIComponent(date)}`;
}

async function renderFilterResponse(request, interaction, action, now) {
  let snapshot = await fetchRunnerSnapshot(snapshotUrl(request));
  let actualDate = reportDateKey(snapshot?.generatedAt);
  // The daily job commits the new snapshot immediately before posting. Vercel
  // may still be deploying for a few seconds, so an exact dated raw snapshot
  // prevents the brand-new Discord message from reading yesterday's static
  // file during that narrow window.
  if (
    action.date !== "latest"
    && action.date !== actualDate
    && !process.env.RUNNER_SNAPSHOT_URL
  ) {
    try {
      const repositorySnapshot = await fetchRunnerSnapshot(repositorySnapshotUrl(action.date));
      const repositoryDate = reportDateKey(repositorySnapshot?.generatedAt);
      if (repositoryDate === action.date) {
        snapshot = repositorySnapshot;
        actualDate = repositoryDate;
      }
    } catch (_) {
      // The same-origin response below still gives a clear stale-report error.
    }
  }
  if (action.date !== "latest" && action.date !== actualDate) {
    return {
      error: `This recap is from ${action.date}; the live index now contains ${actualDate}. Open the newest recap.`,
    };
  }
  const allRows = filterRunnerRows(snapshot, action.chain, action.band);
  const pages = Math.max(1, Math.ceil(allRows.length / PAGE_SIZE));
  const page = Math.min(action.page, pages - 1);
  const rows = allRows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const contracts = rows.map((row) => ({
    chain: String(row.chain).toLowerCase(),
    mint: String(row.mint),
    url: fomoUrl(row),
  }));
  const prices = await pricesForMessage(messageKey(interaction), contracts);
  return {
    embeds: [filteredEmbed(rows, prices, action.chain, action.band, page, pages, allRows.length, now)],
    components: filterComponents(action.chain, action.band, actualDate, page, pages, now),
    allowed_mentions: { parse: [] },
  };
}

export function resetRefreshStateForTests() {
  marketCache.clear();
  messageRefreshes.clear();
  messageLocks.clear();
  snapshotCache = null;
}

function ephemeralMessage(content) {
  return json({ type: 4, data: { content, flags: 64 } });
}

export default {
  async fetch(request) {
    if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
    const rawBody = await request.text();
    const signature = request.headers.get("x-signature-ed25519") || "";
    const timestamp = request.headers.get("x-signature-timestamp") || "";
    const publicKey = process.env.DISCORD_APPLICATION_PUBLIC_KEY || "";
    if (!verifyDiscordRequest(publicKey, signature, timestamp, rawBody)) {
      return json({ error: "invalid request signature" }, 401);
    }

    let interaction;
    try {
      interaction = JSON.parse(rawBody);
    } catch (_) {
      return json({ error: "invalid JSON" }, 400);
    }
    if (interaction.type === 1) return json({ type: 1 });
    const customId = String(interaction?.data?.custom_id || "");
    if (interaction.type !== 3) return ephemeralMessage("Unknown action.");

    const now = Math.floor(Date.now() / 1000);
    const key = messageKey(interaction);
    pruneRefreshState(now);

    const filterAction = parseFilterAction(customId);
    if (filterAction) {
      if (filterAction.refresh) {
        const localRefresh = Number(messageRefreshes.get(key) || 0);
        const remaining = COOLDOWN_SECONDS - (
          now - Math.max(filterAction.refreshedAt, localRefresh)
        );
        if (remaining > 0) {
          return ephemeralMessage(`Live MC was just refreshed. Try again in ${remaining}s.`);
        }
        messageRefreshes.set(key, now);
      }
      try {
        const data = await renderFilterResponse(request, interaction, filterAction, now);
        if (data.error) return ephemeralMessage(data.error);
        const updatingEphemeral = Boolean(Number(interaction?.message?.flags || 0) & 64);
        return json({ type: updatingEphemeral ? 7 : 4, data: {
          ...data,
          ...(updatingEphemeral ? {} : { flags: 64 }),
        } });
      } catch (_) {
        if (filterAction.refresh) messageRefreshes.delete(key);
        return ephemeralMessage("The qualified runner index is temporarily unavailable. Try again shortly.");
      }
    }

    if (!customId.startsWith(REFRESH_PREFIX)) return ephemeralMessage("Unknown action.");
    const refreshParts = customId.slice(REFRESH_PREFIX.length).split(":");
    const lastRefresh = Number(refreshParts[0]) || 0;
    const reportDate = reportDateKey(refreshParts[1]);
    const localRefresh = Number(messageRefreshes.get(key) || 0);
    const remaining = COOLDOWN_SECONDS - (now - Math.max(lastRefresh, localRefresh));
    if (remaining > 0) {
      return ephemeralMessage(`Live MC was just refreshed. Try again in ${remaining}s.`);
    }

    const message = interaction.message || {};
    const embeds = (message.embeds || []).map(editableEmbed);
    const contracts = contractsFromEmbeds(embeds);
    if (!contracts.length) {
      return ephemeralMessage("No exact Fomo contract links were found in this message.");
    }

    messageRefreshes.set(key, now);
    const prices = await pricesForMessage(key, contracts);
    if (!prices.size) {
      messageRefreshes.delete(key);
      return ephemeralMessage("Current market caps are unavailable; the message was not changed.");
    }

    for (const embed of embeds) {
      embed.title = applyLiveCaps(embed.title, contracts, prices);
      embed.description = applyLiveCaps(embed.description, contracts, prices);
      for (const field of embed.fields || []) {
        field.name = applyLiveCaps(field.name, contracts, prices);
        field.value = applyLiveCaps(field.value, contracts, prices);
      }
    }
    if (embeds.length) {
      const existing = String(embeds[embeds.length - 1].footer?.text || "Rolling 24h window");
      const base = existing.split(/\s+(?:·|Â·|Ã‚Â·)\s+MC refreshed/)[0];
      embeds[embeds.length - 1].footer = {
        text: `${base} · MC refreshed ${new Date().toISOString().slice(11, 19)} UTC`,
      };
    }

    const data = {
      embeds,
      components: publicComponents(now, reportDate),
      allowed_mentions: { parse: [] },
    };
    if (Array.isArray(message.attachments) && message.attachments.length) {
      data.attachments = message.attachments.map(({ id, filename }) => ({ id, filename }));
    }
    return json({ type: 7, data });
  },
};
