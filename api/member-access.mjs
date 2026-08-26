import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import Tesseract from "tesseract.js";
import sharp from "sharp";

const COOKIE = "onchain_member";
const SESSION_AGE_SECONDS = 60 * 60 * 24 * 30;
const MAGIC_AGE_SECONDS = 60 * 15;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const LOCAL_STORE = path.join(process.cwd(), "data", "access-local.json");

function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "private, no-store", ...headers },
  });
}

function secret(env = process.env) {
  return String(env.MEMBER_ACCESS_SECRET || env.MARKET_CAP_PROXY_SECRET || env.DISCORD_BOT_TOKEN || "local-member-demo");
}

function normalizeEmail(value) {
  const email = String(value || "").trim().toLowerCase();
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? email : "";
}

function sign(value, key = secret()) {
  return createHmac("sha256", key).update(value).digest("base64url");
}

export function issueToken(kind, email, maxAgeSeconds, now = Date.now(), key = secret()) {
  const payload = Buffer.from(JSON.stringify({
    kind, email, exp: now + maxAgeSeconds * 1000, nonce: randomBytes(8).toString("hex"),
  })).toString("base64url");
  return `${payload}.${sign(payload, key)}`;
}

export function readToken(token, expectedKind, now = Date.now(), key = secret()) {
  const [payload, provided] = String(token || "").split(".");
  if (!payload || !provided) return null;
  const expected = sign(payload, key);
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
  try {
    const value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return value.kind === expectedKind && Number(value.exp) > now && normalizeEmail(value.email) ? value : null;
  } catch (_) {
    return null;
  }
}

export function parseProofText(text, image = {}) {
  const normalized = String(text || "").toLowerCase()
    .replace(/[^a-z0-9@._\s-]/g, " ").replace(/\s+/g, " ");
  const referrer = /account\s+referrer[\s\S]{0,100}@?\s*orangie\b/.test(normalized);
  const accountScreen = /account\s+referrer/.test(normalized)
    && (/manage\s+account/.test(normalized) || /login\s+email/.test(normalized));
  const portraitPhone = Number(image.width) > 0 && Number(image.height) > Number(image.width) * 1.45;
  const phoneChrome = /\b(?:4g|5g|lte)\b/.test(normalized);
  const mobileSignals = [
    /\bfollowing\b[\s\S]{0,40}\bfollowers\b/.test(normalized),
    /\bno\s+hold\s+time\b/.test(normalized) && /\btrades?\b/.test(normalized) && /\bjoined\b/.test(normalized),
    /\bno\s+positions\s+yet\b/.test(normalized) && /\b24h\b[\s\S]{0,40}\b7d\b[\s\S]{0,40}\b30d\b/.test(normalized),
    /\bget\s+your\s+first\s+token\b/.test(normalized) && /\bbuy\s+now\b/.test(normalized),
  ].filter(Boolean).length;
  // The phone app does not expose Account referrer. Accept only a complete,
  // portrait Fomo profile: phone chrome plus at least three independent app
  // sections. A portrait crop of the desktop account modal cannot satisfy it.
  const mobileProfile = portraitPhone && phoneChrome && mobileSignals >= 3;
  const email = normalizeEmail((normalized.match(/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/) || [""])[0]);
  return {
    approved: (referrer && accountScreen) || mobileProfile,
    referrer: referrer ? "@orangie" : mobileProfile ? "mobile-fomo-profile" : "",
    fomoEmail: email,
    proofType: referrer && accountScreen ? "desktop-referral" : mobileProfile ? "mobile-profile" : "unverified",
  };
}

export async function readProof(buffer) {
  const metadata = await sharp(buffer).metadata();
  if (!metadata.width || !metadata.height) throw new Error("Unreadable image");
  const left = Math.floor(metadata.width * 0.38);
  const top = Math.floor(metadata.height * 0.15);
  const width = Math.max(1, Math.floor(metadata.width * 0.24));
  const height = Math.max(1, Math.floor(metadata.height * 0.22));
  const crop = await sharp(buffer)
    .extract({ left, top, width: Math.min(width, metadata.width - left), height: Math.min(height, metadata.height - top) })
    .resize({ width: 1400, withoutEnlargement: false })
    .grayscale().normalize().png().toBuffer();
  const full = await sharp(buffer)
    // Phone screenshots need enlargement for the small activity labels that
    // distinguish a real app profile from a cropped desktop account modal.
    .resize({ width: 2000, withoutEnlargement: false })
    .grayscale().normalize().png().toBuffer();
  const broadLeft = Math.floor(metadata.width * 0.18);
  const broadTop = Math.floor(metadata.height * 0.05);
  const broad = await sharp(buffer)
    .extract({
      left: broadLeft,
      top: broadTop,
      width: Math.max(1, Math.min(Math.floor(metadata.width * 0.64), metadata.width - broadLeft)),
      height: Math.max(1, Math.min(Math.floor(metadata.height * 0.65), metadata.height - broadTop)),
    })
    .resize({ width: 1800, withoutEnlargement: false })
    .grayscale().normalize().png().toBuffer();

  // Screenshots arrive as desktop, mobile, or user-cropped images. Read a
  // tight desktop-modal crop first, then two progressively broader regions.
  // Combining their text lets labels and values recognized in different
  // passes still validate as one proof.
  const texts = [];
  for (const candidate of [crop, broad, full]) {
    const result = await Tesseract.recognize(candidate, "eng");
    texts.push(result.data.text);
    const parsed = parseProofText(texts.join("\n"), metadata);
    if (parsed.approved) return parsed;
  }
  return parseProofText(texts.join("\n"), metadata);
}

async function localRead() {
  try { return JSON.parse(await readFile(LOCAL_STORE, "utf8")); }
  catch (_) { return { members: {}, proofs: {} }; }
}

async function localWrite(store) {
  await mkdir(path.dirname(LOCAL_STORE), { recursive: true });
  await writeFile(LOCAL_STORE, `${JSON.stringify(store, null, 2)}\n`, "utf8");
}

function supabaseConfig(env = process.env) {
  const url = String(env.SUPABASE_URL || "").replace(/\/$/, "");
  const key = String(env.SUPABASE_SECRET_KEY || "");
  return url && key ? { url, key } : null;
}

async function supabaseRequest(pathname, options = {}) {
  const config = supabaseConfig();
  if (!config) return null;
  const authHeaders = config.key.startsWith("sb_secret_")
    ? { apikey: config.key }
    : { apikey: config.key, authorization: `Bearer ${config.key}` };
  const response = await fetch(`${config.url}/rest/v1/${pathname}`, {
    ...options,
    headers: {
      ...authHeaders,
      "content-type": "application/json",
      ...options.headers,
    },
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.message || detail.details || `Supabase request failed (${response.status})`);
    error.status = response.status;
    error.code = detail.code;
    throw error;
  }
  if (response.status === 204) return null;
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

async function getMember(email) {
  if (supabaseConfig()) {
    const rows = await supabaseRequest(`members?email=eq.${encodeURIComponent(email)}&select=*`);
    const member = rows?.[0];
    return member ? {
      email: member.email,
      approved: member.approved,
      approvedAt: member.approved_at,
      proofHash: member.proof_hash,
      verifiedReferrer: member.verified_referrer,
      fomoEmail: member.fomo_email,
    } : null;
  }
  return (await localRead()).members[email] || null;
}

async function approveMember(email, proofHash, proof) {
  if (supabaseConfig()) {
    try {
      await supabaseRequest("members", {
        method: "POST",
        headers: { prefer: "return=minimal" },
        body: JSON.stringify({
          email,
          approved: true,
          approved_at: new Date().toISOString(),
          proof_hash: proofHash,
          verified_referrer: proof.referrer,
          fomo_email: proof.fomoEmail || null,
        }),
      });
      return;
    } catch (error) {
      if (error.status === 409 || error.code === "23505") {
        if (await getMember(email)) throw new Error("This email is already registered. Use Already approved to sign in.");
        throw new Error("This proof was already used");
      }
      throw error;
    }
  }
  const store = await localRead();
  if (store.members[email]) {
    throw new Error("This email is already registered. Use Already approved to sign in.");
  }
  const production = process.env.VERCEL_ENV === "production";
  if (production && store.proofs[proofHash] && store.proofs[proofHash] !== email) {
    throw new Error("This proof was already used");
  }
  // Let the supplied sample proof be exercised repeatedly in local previews.
  // Production continues to enforce one proof per approved membership.
  if (!production && store.proofs[proofHash] && store.proofs[proofHash] !== email) {
    delete store.members[store.proofs[proofHash]];
  }
  store.proofs[proofHash] = email;
  store.members[email] = {
    email, approved: true, approvedAt: new Date().toISOString(), proofHash,
    verifiedReferrer: proof.referrer, fomoEmail: proof.fomoEmail,
  };
  await localWrite(store);
}

function cookieValue(request) {
  const match = String(request.headers.get("cookie") || "").match(new RegExp(`(?:^|;\\s*)${COOKIE}=([^;]+)`));
  return match ? decodeURIComponent(match[1]) : "";
}

function sessionCookie(token) {
  return `${COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${SESSION_AGE_SECONDS}`;
}

async function approvedSession(request) {
  const session = readToken(cookieValue(request), "session");
  if (!session) return null;
  const member = await getMember(session.email);
  return member?.approved ? member : null;
}

async function handleVerify(request) {
  const form = await request.formData();
  const email = normalizeEmail(form.get("newsletterEmail"));
  const image = form.get("proof");
  if (!email) return json({ error: "Enter a valid newsletter email" }, 400);
  if (await getMember(email)) {
    return json({ error: "This email is already registered. Use Already approved to sign in." }, 409);
  }
  if (!(image instanceof Blob) || !image.size || image.size > MAX_IMAGE_BYTES) {
    return json({ error: "Upload a screenshot under 8 MB" }, 400);
  }
  const buffer = Buffer.from(await image.arrayBuffer());
  const proofHash = createHash("sha256").update(buffer).digest("hex");
  const proof = await readProof(buffer);
  if (!proof.approved) return json({ error: "Upload either the full Fomo mobile profile or the desktop account page showing Account referrer @orangie" }, 422);
  await approveMember(email, proofHash, proof);
  const session = issueToken("session", email, SESSION_AGE_SECONDS);
  return json({
    approved: true, email, detectedFomoEmail: proof.fomoEmail || null,
    redirect: "/member-site",
  }, 200, { "set-cookie": sessionCookie(session) });
}

async function handleLogin(request) {
  const email = normalizeEmail((await request.json()).email);
  const member = email ? await getMember(email) : null;
  if (!member?.approved) return json({ error: "That email has not been approved" }, 403);
  const magic = issueToken("magic", email, MAGIC_AGE_SECONDS);
  return json({ sent: false, demo: true, magicUrl: `/api/member-access?action=redeem&token=${encodeURIComponent(magic)}` });
}

async function handleRedeem(request) {
  const magic = readToken(new URL(request.url).searchParams.get("token"), "magic");
  if (!magic || !(await getMember(magic.email))?.approved) return json({ error: "Invalid or expired login link" }, 401);
  const session = issueToken("session", magic.email, SESSION_AGE_SECONDS);
  return new Response(null, {
    status: 302,
    headers: { location: "/member-site", "set-cookie": sessionCookie(session), "cache-control": "private, no-store" },
  });
}

async function handleSite(request) {
  if (!(await approvedSession(request))) {
    return new Response(null, {
      status: 302,
      headers: { location: "/join", "cache-control": "private, no-store" },
    });
  }
  const source = await readFile(path.join(process.cwd(), "web", "index.html"), "utf8");
  const protectedSource = source.replace(
    'fetch("data/latest.json", { cache: "no-store" })',
    'fetch("/api/member-access?action=feed", { cache: "no-store" })',
  );
  return new Response(protectedSource, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "private, no-store",
      "x-robots-tag": "noindex, nofollow",
    },
  });
}

async function handleFeed(request) {
  if (!(await approvedSession(request))) return json({ error: "Authentication required" }, 401);
  const data = await readFile(path.join(process.cwd(), "web", "data", "latest.json"), "utf8");
  return new Response(data, { headers: { "content-type": "application/json", "cache-control": "private, no-store" } });
}

export async function handleMemberAccess(request) {
  const action = new URL(request.url).searchParams.get("action") || "status";
  try {
    if (request.method === "POST" && action === "verify") return await handleVerify(request);
    if (request.method === "POST" && action === "login") return await handleLogin(request);
    if (request.method === "GET" && action === "redeem") return await handleRedeem(request);
    if (request.method === "GET" && action === "feed") return await handleFeed(request);
    if (request.method === "GET" && action === "site") return await handleSite(request);
    if (request.method === "GET" && action === "status") {
      const member = await approvedSession(request);
      return json({ authenticated: Boolean(member), email: member?.email || null });
    }
    return json({ error: "Not found" }, 404);
  } catch (error) {
    return json({ error: error?.message || "Access request failed" }, 400);
  }
}

export default { fetch: handleMemberAccess };
