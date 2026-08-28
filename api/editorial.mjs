import { createHmac, timingSafeEqual } from "node:crypto";
import { baseSnapshot, mergedSnapshot, readEditorialState, runnerKey, writeEditorialState } from "./editorial-store.mjs";
import { filterRunnerRows, polishedLeadPayload } from "./discord-interactions.mjs";
import { telegramKeyboard, telegramText } from "./telegram-interactions.mjs";
import { researchManualRunner } from "./manual-runner.mjs";

const COOKIE = "onchain_admin";
const MAX_AGE = 60 * 60 * 12;
const EDITABLE = new Set(["symbol", "name", "lore"]);

function response(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "private, no-store", ...headers } });
}
function secret() { return String(process.env.MEMBER_ACCESS_SECRET || process.env.DISCORD_BOT_TOKEN || ""); }
function signature(value) { return createHmac("sha256", secret()).update(value).digest("base64url"); }
function equalSecret(supplied, expected) {
  if (!expected || supplied.length !== expected.length) return false;
  return timingSafeEqual(Buffer.from(supplied), Buffer.from(expected));
}
function adminAccounts() {
  try {
    const accounts = JSON.parse(String(process.env.ADMIN_ACCOUNTS_JSON || "{}"));
    return accounts && typeof accounts === "object" && !Array.isArray(accounts) ? accounts : {};
  } catch {
    return {};
  }
}
function validAdmin(username, password) {
  const entry = Object.entries(adminAccounts()).find(([name]) => String(name).trim().toLowerCase() === username);
  if (entry && typeof entry[1] === "string") return equalSecret(password, entry[1]);
  const fallbackUsername = String(process.env.ADMIN_USERNAME || "").trim().toLowerCase();
  return equalSecret(username, fallbackUsername) && equalSecret(password, String(process.env.ADMIN_PASSWORD || ""));
}
function token(username) { const value = `${Date.now() + MAX_AGE * 1000}.${Math.random().toString(36).slice(2)}.${username}`; return `${value}.${signature(value)}`; }
function authorized(request) {
  const raw = String(request.headers.get("cookie") || "").match(/(?:^|;\s*)onchain_admin=([^;]+)/)?.[1] || "";
  const [expires, nonce, username, supplied] = decodeURIComponent(raw).split(".");
  if (!expires || !nonce || !username || !supplied || Number(expires) < Date.now() || !secret()) return "";
  const expected = signature(`${expires}.${nonce}.${username}`);
  const a = Buffer.from(supplied), b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b) ? username : "";
}
function cleanPatch(value = {}) {
  return Object.fromEntries(Object.entries(value).filter(([key]) => EDITABLE.has(key)).map(([key, item]) => [key, String(item ?? "").trim()]));
}
function reportState(base, state) {
  return state.reportId === base.generatedAt ? state : { reportId: base.generatedAt, overrides: {}, hidden: [], added: [], audit: [], publications: {} };
}
const BOARD_FIELDS = ["chain", "mint", "symbol", "name", "marketCap", "observedPeakMarketCap", "peakMarketCap", "athVerified", "liquidity", "volume24h", "change24h", "startMarketCap", "ageHours", "holders", "top10Pct", "lpLockedPct", "lore", "recapCategory", "manualImport", "riskLabels", "rugged", "freezeAuthorityDisabled", "mintAuthorityRenounced", "scores", "providerEvidence"];
function boardSnapshot(snapshot) {
  const rows = snapshot.runnerUniverse || snapshot.runners || [];
  return { generatedAt: snapshot.generatedAt, runnerUniverse: rows.map((row) => Object.fromEntries(BOARD_FIELDS.filter((key) => row?.[key] !== undefined).map((key) => [key, row[key]]))) };
}

async function publishDiscord(snapshot, state) {
  const bot = String(process.env.DISCORD_BOT_TOKEN || ""), channel = String(process.env.DISCORD_CHANNEL_ID || "").split(",")[0].trim();
  if (!bot || !channel) return { skipped: "Discord credentials unavailable" };
  const payload = polishedLeadPayload(snapshot, 12);
  const prior = state.publications?.discord;
  const url = prior ? `https://discord.com/api/v10/channels/${channel}/messages/${prior}` : `https://discord.com/api/v10/channels/${channel}/messages`;
  const res = await fetch(url, { method: prior ? "PATCH" : "POST", headers: { authorization: `Bot ${bot}`, "content-type": "application/json" }, body: JSON.stringify(payload) });
  if (!res.ok) throw new Error(`Discord publish failed (${res.status})`);
  const data = await res.json(); return { messageId: data.id };
}
async function publishTelegram(snapshot, state) {
  const bot = String(process.env.TELEGRAM_BOT_TOKEN || ""), chat = String(process.env.TELEGRAM_CHAT_ID || "");
  if (!bot || !chat) return { skipped: "Telegram credentials unavailable" };
  const all = filterRunnerRows(snapshot), rows = all.slice(0, 8), pages = Math.max(1, Math.ceil(all.length / 8));
  const payload = { chat_id: chat, parse_mode: "HTML", disable_web_page_preview: true, text: telegramText(rows, new Map(), "all", "all", 0, pages, all.length, Date.now()), reply_markup: telegramKeyboard(snapshot, "all", "all", 0, pages, String(snapshot.generatedAt).slice(0, 10).replaceAll("-", ""), process.env.REPORT_URL || "https://onchainnews-rho.vercel.app") };
  const prior = state.publications?.telegram;
  if (prior) payload.message_id = prior;
  const res = await fetch(`https://api.telegram.org/bot${bot}/${prior ? "editMessageText" : "sendMessage"}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  if (!res.ok) throw new Error(`Telegram publish failed (${res.status})`);
  const data = await res.json(); return { messageId: data.result.message_id };
}
async function publishEverywhere(snapshot, state) {
  const [discord, telegram] = await Promise.all([publishDiscord(snapshot, state), publishTelegram(snapshot, state)]);
  state.publications = { ...(state.publications || {}), ...(discord.messageId ? { discord: discord.messageId } : {}), ...(telegram.messageId ? { telegram: telegram.messageId } : {}), publishedAt: new Date().toISOString() };
  await writeEditorialState(state);
  return { discord, telegram };
}

export async function handleEditorial(request) {
  const action = new URL(request.url).searchParams.get("action") || "status";
  try {
    if (request.method === "GET" && action === "snapshot") return response(await mergedSnapshot(), 200, { "cache-control": "public, max-age=0, s-maxage=15" });
    if (request.method === "GET" && action === "board") return response(boardSnapshot(await mergedSnapshot()), 200, { "cache-control": "public, max-age=0, s-maxage=30, stale-while-revalidate=60" });
    if (request.method === "POST" && action === "login") {
      const body = await request.json();
      const suppliedUsername = String(body.username || "").trim().toLowerCase();
      const suppliedPassword = String(body.password || "");
      if (!validAdmin(suppliedUsername, suppliedPassword)) return response({ error: "Invalid username or password" }, 401);
      const base = await baseSnapshot();
      const state = reportState(base, await readEditorialState());
      const at = new Date().toISOString();
      state.audit = [{ at, operation: "login", actor: suppliedUsername, key: "admin" }, ...(state.audit || [])].slice(0, 250);
      await writeEditorialState(state);
      return response({ authenticated: true, actor: suppliedUsername }, 200, { "set-cookie": `${COOKIE}=${encodeURIComponent(token(suppliedUsername))}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${MAX_AGE}` });
    }
    if (request.method === "POST" && action === "logout") return response({ authenticated: false }, 200, { "set-cookie": `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0` });
    const actor = authorized(request);
    if (!actor) return response({ error: "Authentication required" }, 401);
    if (request.method === "GET" && action === "status") return response({ authenticated: true, actor });
    const base = await baseSnapshot();
    let state = reportState(base, await readEditorialState());
    if (request.method === "GET" && action === "state") return response({ state, snapshot: await mergedSnapshot() });
    if (request.method === "POST" && action === "import") {
      const body = await request.json();
      const runner = await researchManualRunner(body.chain, body.mint);
      const key = runnerKey(runner);
      const baseRows = [...(base.runnerUniverse || []), ...(base.runners || [])];
      const exists = baseRows.some((item) => runnerKey(item) === key);
      state.hidden = (state.hidden || []).filter((item) => item !== key);
      if (exists) state.overrides = { ...(state.overrides || {}), [key]: runner };
      else state.added = [...(state.added || []).filter((item) => runnerKey(item) !== key), runner];
      state.updatedAt = new Date().toISOString();
      state.audit = [{ at: state.updatedAt, operation: "import", actor, key }, ...(state.audit || [])].slice(0, 250);
      await writeEditorialState(state);
      const snapshot = await mergedSnapshot();
      const publications = await publishEverywhere(snapshot, state);
      return response({ imported: true, runner, snapshot, ...publications, runnerCount: filterRunnerRows(snapshot).length });
    }
    if (request.method === "POST" && action === "save") {
      const body = await request.json(), key = String(body.key || "").toLowerCase();
      if (body.operation === "hide") state.hidden = [...new Set([...(state.hidden || []), key])];
      else if (body.operation === "restore") state.hidden = (state.hidden || []).filter((item) => item !== key);
      else if (body.operation === "add") { const row = { ...body.runner, chain: String(body.runner?.chain || "").toLowerCase(), mint: String(body.runner?.mint || "").trim() }; if (!row.chain || !row.mint || !row.symbol) return response({ error: "Chain, contract and ticker are required" }, 400); state.added = [...(state.added || []).filter((item) => runnerKey(item) !== runnerKey(row)), row]; }
      else { state.overrides = { ...(state.overrides || {}), [key]: { ...(state.overrides?.[key] || {}), ...cleanPatch(body.patch) } }; }
      state.updatedAt = new Date().toISOString();
      state.audit = [{ at: state.updatedAt, operation: body.operation || "edit", actor, key }, ...(state.audit || [])].slice(0, 250);
      await writeEditorialState(state); return response({ saved: true, snapshot: await mergedSnapshot() });
    }
    if (request.method === "POST" && action === "publish") {
      const snapshot = await mergedSnapshot();
      state.updatedAt = new Date().toISOString();
      state.audit = [{ at: state.updatedAt, operation: "publish", actor, key: "all-platforms" }, ...(state.audit || [])].slice(0, 250);
      await writeEditorialState(state);
      const publications = await publishEverywhere(snapshot, state);
      return response({ published: true, ...publications, runnerCount: filterRunnerRows(snapshot).length });
    }
    return response({ error: "Not found" }, 404);
  } catch (error) { return response({ error: error?.message || "Editorial request failed" }, 400); }
}

export default { fetch: handleEditorial };
