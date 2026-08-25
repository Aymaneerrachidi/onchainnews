import assert from "node:assert/strict";
import test from "node:test";

import {
  parseTelegramAction,
  telegramKeyboard,
  telegramText,
} from "../api/telegram-interactions.mjs";

function runner(symbol, mint, chain, peak) {
  return {
    symbol, mint, chain,
    marketCap: peak * 0.8,
    observedPeakMarketCap: peak,
    liquidity: 100000,
    holders: 1200,
    top10Pct: 12,
    lore: `${symbol} lore`,
    ageHours: 8,
    trades24h: 500,
    securityFlags: [],
    rugged: false,
    lpLockedPct: 100,
    mintAuthorityRenounced: true,
    freezeAuthorityDisabled: true,
    providerEvidence: { gmgn: { washTrading: false } },
  };
}

test("Telegram callback data parses and remains under Telegram's limit", () => {
  const action = parseTelegramAction("tg|refresh|solana|500k-1m|20260826|3");
  assert.deepEqual(action, { action: "refresh", chain: "solana", band: "500k-1m", date: "20260826", page: 3 });
  assert.equal(parseTelegramAction("bad"), null);
  const snapshot = { runnerUniverse: [runner("ONE", "mint", "solana", 600000)] };
  const keyboard = telegramKeyboard(snapshot, "all", "all", 0, 1, "20260826", "https://example.com");
  const callbacks = keyboard.inline_keyboard.flat().map((button) => button.callback_data).filter(Boolean);
  assert.equal(callbacks.every((value) => Buffer.byteLength(value) <= 64), true);
});

test("Telegram detail text uses Fomo links and enriched lore", () => {
  const item = runner("ONE", "mint", "solana", 600000);
  item.xInteractions = [{ handle: "real", summary: "Fresh attributable X context.", url: "https://x.com/real/status/1" }];
  const text = telegramText([item], new Map(), "solana", "500k-1m", 0, 1, 1, Date.UTC(2026, 7, 26));
  assert.match(text, /fomo\.family\/tokens\/solana\/mint/);
  assert.match(text, /Fresh attributable X context/);
  assert.match(text, /source/);
});
