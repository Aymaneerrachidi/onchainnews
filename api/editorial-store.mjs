import { readFile } from "node:fs/promises";
import path from "node:path";

const STATE_EMAIL = "editorial-console@system.local";

function config(env = process.env) {
  const url = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = String(env.SUPABASE_SECRET_KEY || "");
  return url && key ? { url, key } : null;
}

async function request(pathname, options = {}) {
  const value = config();
  if (!value) throw new Error("Supabase is required for editorial overrides");
  const response = await fetch(`${value.url}/rest/v1/${pathname}`, {
    ...options,
    headers: {
      apikey: value.key,
      ...(value.key.startsWith("sb_secret_") ? {} : { authorization: `Bearer ${value.key}` }),
      "content-type": "application/json",
      ...options.headers,
    },
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.message || detail.details || `Supabase request failed (${response.status})`);
  }
  if (response.status === 204) return null;
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

export async function baseSnapshot() {
  return JSON.parse(await readFile(path.join(process.cwd(), "web", "data", "latest.json"), "utf8"));
}

export async function readEditorialState() {
  if (!config()) return { reportId: "", overrides: {}, hidden: [], added: [], audit: [], publications: {} };
  const rows = await request(`members?email=eq.${encodeURIComponent(STATE_EMAIL)}&select=proof_hash`);
  try {
    return rows?.[0]?.proof_hash ? JSON.parse(rows[0].proof_hash) : { reportId: "", overrides: {}, hidden: [], added: [], audit: [], publications: {} };
  } catch (_) {
    return { reportId: "", overrides: {}, hidden: [], added: [], audit: [], publications: {} };
  }
}

export async function writeEditorialState(state) {
  const payload = {
    email: STATE_EMAIL,
    approved: false,
    approved_at: new Date().toISOString(),
    proof_hash: JSON.stringify(state),
    verified_referrer: "editorial-admin-state",
    fomo_email: null,
  };
  await request("members?on_conflict=email", {
    method: "POST",
    headers: { prefer: "resolution=merge-duplicates,return=minimal" },
    body: JSON.stringify(payload),
  });
}

export function runnerKey(row) {
  return `${String(row?.chain || "").toLowerCase()}:${String(row?.mint || "").toLowerCase()}`;
}

export function mergeEditorialState(snapshot, state) {
  if (!state || state.reportId !== snapshot.generatedAt) return snapshot;
  const hidden = new Set(state.hidden || []);
  const overrides = state.overrides || {};
  const mergeRows = (rows = [], includeAdded = true) => {
    const seen = new Set();
    const merged = [];
    for (const row of [...rows, ...(includeAdded ? state.added || [] : [])]) {
      const key = runnerKey(row);
      if (!key || hidden.has(key) || seen.has(key)) continue;
      seen.add(key);
      merged.push({ ...row, ...(overrides[key] || {}), editorialOverride: Boolean(overrides[key]) });
    }
    return merged;
  };
  const result = structuredClone(snapshot);
  result.runnerUniverse = mergeRows(snapshot.runnerUniverse || snapshot.runners || []);
  result.runners = mergeRows(snapshot.runners || snapshot.runnerUniverse || []);
  result.discordPublishedRunners = mergeRows(snapshot.discordPublishedRunners || snapshot.runnerUniverse || snapshot.runners || []);
  if (result.recap && typeof result.recap === "object") {
    for (const [name, rows] of Object.entries(result.recap)) {
      if (Array.isArray(rows)) result.recap[name] = mergeRows(rows, name === "all");
    }
    result.recap.all = result.runnerUniverse;
  }
  result.editorial = { active: true, updatedAt: state.updatedAt || null };
  if (result.summary) result.summary.runnerCount = result.runnerUniverse.length;
  return result;
}

export async function mergedSnapshot() {
  const [snapshot, state] = await Promise.all([baseSnapshot(), readEditorialState()]);
  return mergeEditorialState(snapshot, state);
}
