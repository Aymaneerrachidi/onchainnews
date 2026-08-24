import { createPublicKey, verify } from "node:crypto";

const DISCORD_PUBLIC_KEY_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const REFRESH_PREFIX = "refresh_mc:";
const COOLDOWN_SECONDS = 30;
const FOMO_TOKEN = /https:\/\/fomo\.family\/tokens\/([^/\s)]+)\/([^\s)]+)/g;

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

export function contractsFromEmbeds(embeds = []) {
  const contracts = new Map();
  const inspect = (text) => {
    for (const match of String(text || "").matchAll(FOMO_TOKEN)) {
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
  return [...contracts.values()];
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
  const updated = /\s*·\s*now\s+(?:\$[\d.,]+[KMB]?|n\/a|unavailable)/i.test(result)
    ? result.replace(/\s*·\s*now\s+(?:\$[\d.,]+[KMB]?|n\/a|unavailable)/i, ` ${live}`)
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
  return Object.fromEntries(allowed.filter((key) => embed?.[key] !== undefined).map((key) => [key, embed[key]]));
}

async function fetchLiveCaps(contracts) {
  const grouped = new Map();
  for (const contract of contracts) {
    if (!grouped.has(contract.chain)) grouped.set(contract.chain, []);
    grouped.get(contract.chain).push(contract);
  }
  const prices = new Map();
  await Promise.all([...grouped.entries()].flatMap(([chain, rows]) => {
    const jobs = [];
    for (let start = 0; start < rows.length; start += 30) {
      const batch = rows.slice(start, start + 30);
      const addresses = batch.map((row) => encodeURIComponent(row.mint)).join(",");
      jobs.push(fetch(`https://api.dexscreener.com/tokens/v1/${encodeURIComponent(chain)}/${addresses}`, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(2200),
      }).then((response) => response.ok ? response.json() : [])
        .then((pairs) => {
          for (const contract of batch) {
            const matches = (Array.isArray(pairs) ? pairs : []).filter((pair) =>
              String(pair?.baseToken?.address || "").toLowerCase() === contract.mint.toLowerCase()
              && Number(pair?.marketCap) > 0
            ).sort((a, b) => Number(b?.liquidity?.usd || 0) - Number(a?.liquidity?.usd || 0));
            if (matches.length) {
              prices.set(`${chain}:${contract.mint.toLowerCase()}`, Number(matches[0].marketCap));
            }
          }
        }).catch(() => null));
    }
    return jobs;
  }));
  return prices;
}

function refreshComponents(epochSeconds) {
  return [{
    type: 1,
    components: [{
      type: 2,
      style: 2,
      label: "Refresh live MC",
      custom_id: `${REFRESH_PREFIX}${epochSeconds}`,
    }],
  }];
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
    if (interaction.type !== 3 || !customId.startsWith(REFRESH_PREFIX)) {
      return json({
        type: 4,
        data: { content: "Unknown action.", flags: 64 },
      });
    }

    const now = Math.floor(Date.now() / 1000);
    const lastRefresh = Number(customId.slice(REFRESH_PREFIX.length)) || 0;
    const remaining = COOLDOWN_SECONDS - (now - lastRefresh);
    if (remaining > 0) {
      return json({
        type: 4,
        data: { content: `Live MC was just refreshed. Try again in ${remaining}s.`, flags: 64 },
      });
    }

    const message = interaction.message || {};
    const embeds = (message.embeds || []).map(editableEmbed);
    const contracts = contractsFromEmbeds(embeds);
    const prices = await fetchLiveCaps(contracts);
    if (!prices.size) {
      return json({
        type: 4,
        data: { content: "Current market caps are unavailable; the message was not changed.", flags: 64 },
      });
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
      const base = existing.split(" · MC refreshed")[0];
      embeds[embeds.length - 1].footer = {
        text: `${base} · MC refreshed ${new Date().toISOString().slice(11, 19)} UTC`,
      };
    }

    const data = {
      embeds,
      components: refreshComponents(now),
      allowed_mentions: { parse: [] },
    };
    if (Array.isArray(message.attachments) && message.attachments.length) {
      data.attachments = message.attachments.map(({ id, filename }) => ({ id, filename }));
    }
    return json({ type: 7, data });
  },
};
