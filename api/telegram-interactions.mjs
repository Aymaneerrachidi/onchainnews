import {
  fetchLiveCaps,
  fetchRunnerSnapshot,
  filterRunnerRows,
  fomoUrl,
  narrativeSource,
  runnerPeak,
  shortCause,
  sharedMarketFetch,
} from "./discord-interactions.mjs";

const PAGE_SIZE = 8;
const CHAINS = [["All", "all"], ["Solana", "solana"], ["BNB", "bsc"], ["Base", "base"], ["ETH", "ethereum"], ["Robinhood", "robinhood"]];
const BANDS = [["All caps", "all"], ["$250K–$500K", "250k-500k"], ["$500K–$1M", "500k-1m"], ["$1M–$10M", "1m-10m"], ["$10M+", "10m-plus"]];
const CHAIN_LABELS = new Map(CHAINS.map(([label, value]) => [value, label]));
const BAND_LABELS = new Map(BANDS.map(([label, value]) => [value, label]));

function money(value) {
  const number = Number(value) || 0;
  const absolute = Math.abs(number);
  if (absolute >= 1e9) return `$${(number / 1e9).toFixed(1)}B`;
  if (absolute >= 1e6) return `$${(number / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `$${Math.round(number / 1e3)}K`;
  return `$${Math.round(number)}`;
}

function count(value) {
  return Math.max(0, Number(value) || 0).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function reportDateKey(value) {
  const match = String(value || "").match(/^(\d{4})-?(\d{2})-?(\d{2})/);
  return match ? `${match[1]}${match[2]}${match[3]}` : "latest";
}

function callbackData(chain, band, date, page, action = "filter") {
  return `tg|${action}|${chain}|${band}|${date}|${page}`;
}

export function parseTelegramAction(value) {
  const parts = String(value || "").split("|");
  if (parts.length !== 6 || parts[0] !== "tg") return null;
  const [, action, chain, band, date, rawPage] = parts;
  if (!["filter", "refresh"].includes(action) || !CHAIN_LABELS.has(chain) || !BAND_LABELS.has(band)) return null;
  return { action, chain, band, date: reportDateKey(date), page: Math.max(0, Number.parseInt(rawPage, 10) || 0) };
}

export function telegramKeyboard(snapshot, chain = "all", band = "all", page = 0, pages = 1, date = "latest", reportUrl = "") {
  const button = (text, nextChain, nextBand, nextPage = 0) => ({
    text: `${nextChain === chain && nextBand === band ? "● " : ""}${text}`,
    callback_data: callbackData(nextChain, nextBand, date, nextPage),
  });
  const chainButtons = CHAINS.map(([label, value]) => {
    const total = filterRunnerRows(snapshot, value, band).length;
    return button(`${label} ${total}`, value, band);
  });
  const bandButtons = BANDS.map(([label, value]) => {
    const total = filterRunnerRows(snapshot, chain, value).length;
    return button(`${label} ${total}`, chain, value);
  });
  const rows = [chainButtons.slice(0, 3), chainButtons.slice(3), bandButtons.slice(0, 3), bandButtons.slice(3)];
  rows.push([
    { text: "‹ Prev", callback_data: callbackData(chain, band, date, Math.max(0, page - 1)) },
    { text: `${page + 1}/${pages}`, callback_data: callbackData(chain, band, date, page) },
    { text: "Next ›", callback_data: callbackData(chain, band, date, Math.min(pages - 1, page + 1)) },
    { text: "↻ MC", callback_data: callbackData(chain, band, date, page, "refresh") },
  ]);
  if (reportUrl) rows.push([{ text: "Open full website", url: reportUrl }]);
  return { inline_keyboard: rows };
}

export function telegramText(rows, prices, chain, band, page, pages, total, refreshedAt, notice = "") {
  const title = `${CHAIN_LABELS.get(chain) || chain} · ${BAND_LABELS.get(band) || band}`;
  const lines = rows.map((row) => {
    const key = `${String(row.chain).toLowerCase()}:${String(row.mint).toLowerCase()}`;
    const current = prices.get(key) || Number(row.marketCap) || 0;
    const holders = row?.holders == null ? "holders unknown" : `${count(row.holders)} holders`;
    const top10 = row?.top10Pct == null ? "top10 unknown" : `top10 ${Number(row.top10Pct).toFixed(1)}%`;
    const source = narrativeSource(row);
    const lore = escapeHtml(shortCause(row));
    const sourceLink = source ? ` · <a href="${escapeHtml(source)}">source</a>` : "";
    return `<a href="${escapeHtml(fomoUrl(row))}"><b>$${escapeHtml(String(row.symbol || "?").toUpperCase())}</b></a> — <b>now ${money(current)}</b> · high ${money(runnerPeak(row))} · liq ${money(row.liquidity)} · ${holders} · ${top10}\n${lore}${sourceLink}`;
  });
  return [
    "🟣 <b>FOMO ONCHAIN · DAILY RUNNERS</b>",
    `<b>${escapeHtml(title)}</b> · full 24h tape`,
    notice ? `<i>${escapeHtml(notice)}</i>` : "",
    lines.join("\n\n") || "No qualified runners match this filter.",
    `<i>${total} screened runners · page ${page + 1}/${pages} · MC ${new Date(refreshedAt).toISOString().slice(11, 19)} UTC</i>`,
  ].filter(Boolean).join("\n\n");
}

function snapshotUrl(request) {
  return String(process.env.RUNNER_SNAPSHOT_URL || new URL("/api/editorial?action=snapshot", request.url));
}

export async function renderTelegramResponse(request, action, fetchImpl = fetch) {
  const snapshot = await fetchRunnerSnapshot(snapshotUrl(request), fetchImpl);
  const date = reportDateKey(snapshot?.generatedAt);
  let chain = action.chain;
  let band = action.band;
  let allRows = filterRunnerRows(snapshot, chain, band);
  let notice = "";
  if (!allRows.length && chain !== "all" && band !== "all") {
    band = "all";
    allRows = filterRunnerRows(snapshot, chain, band);
    notice = "No runners matched both filters, so the cap range was reset.";
  }
  const pages = Math.max(1, Math.ceil(allRows.length / PAGE_SIZE));
  const page = Math.min(action.page, pages - 1);
  const rows = allRows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  let prices = new Map();
  if (action.action === "refresh") {
    prices = await fetchLiveCaps(rows.map((row) => ({ chain: row.chain, mint: row.mint, url: fomoUrl(row) })), sharedMarketFetch(request.url, fetchImpl));
  }
  return {
    text: telegramText(rows, prices, chain, band, page, pages, allRows.length, Date.now(), notice),
    reply_markup: telegramKeyboard(snapshot, chain, band, page, pages, date, process.env.REPORT_URL || "https://onchainnews-rho.vercel.app"),
  };
}

async function telegram(method, payload, fetchImpl = fetch) {
  const token = String(process.env.TELEGRAM_BOT_TOKEN || "");
  if (!token) throw new Error("TELEGRAM_BOT_TOKEN is unset");
  const response = await fetchImpl(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`Telegram ${method} failed (${response.status})`);
}

export default {
  async fetch(request) {
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
    const expectedSecret = String(process.env.TELEGRAM_WEBHOOK_SECRET || "");
    const receivedSecret = request.headers.get("x-telegram-bot-api-secret-token") || "";
    if (!expectedSecret || receivedSecret !== expectedSecret) return new Response("Unauthorized", { status: 401 });
    const update = await request.json();
    const query = update?.callback_query;
    const action = parseTelegramAction(query?.data);
    if (!query || !action) return new Response("ok");
    try {
      const rendered = await renderTelegramResponse(request, action);
      await telegram("editMessageText", {
        chat_id: query.message.chat.id,
        message_id: query.message.message_id,
        parse_mode: "HTML",
        disable_web_page_preview: true,
        ...rendered,
      });
      await telegram("answerCallbackQuery", { callback_query_id: query.id });
    } catch (error) {
      await telegram("answerCallbackQuery", { callback_query_id: query.id, text: "Could not refresh this view. Try again.", show_alert: false });
    }
    return new Response("ok");
  },
};
