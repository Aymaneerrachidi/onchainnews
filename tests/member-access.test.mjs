import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { issueToken, parseProofText, readProof, readToken } from "../api/member-access.mjs";

test("accepts the expected Fomo account proof", () => {
  const proof = parseProofText("Manage account Login email member@example.com Account referrer @orangie");
  assert.equal(proof.approved, true);
  assert.equal(proof.referrer, "@orangie");
  assert.equal(proof.fomoEmail, "member@example.com");
});

test("accepts OCR spacing around the referrer handle", () => {
  const proof = parseProofText("Login email member@example.com Account referrer @ orangie");
  assert.equal(proof.approved, true);
});

test("rejects a different referrer and incomplete screenshots", () => {
  assert.equal(parseProofText("Manage account Login email member@example.com Account referrer @someone").approved, false);
  assert.equal(parseProofText("Account referrer @orangie").approved, false);
});

test("accepts a complete Fomo mobile profile without a desktop referrer", () => {
  const text = "4G Gyro @Gyrotrenches 4 Following 0 Followers No hold time 0 trades Joined Jul 2026 24h 7d 30d No positions yet Get your first token with Pay Buy now";
  const proof = parseProofText(text, { width: 768, height: 1536 });
  assert.equal(proof.approved, true);
  assert.equal(proof.proofType, "mobile-profile");
});

test("does not mistake a cropped desktop proof for a mobile profile", () => {
  const cropped = parseProofText("Manage account Login email member@example.com Account referrer @someone", { width: 700, height: 1400 });
  assert.equal(cropped.approved, false);
  const incompletePhone = parseProofText("4G Following Followers 24h 7d 30d", { width: 768, height: 1536 });
  assert.equal(incompletePhone.approved, false);
});

const localProof = new URL("../new/fomo.webp", import.meta.url);
const mobileProof = new URL("../new/profile phone.jpeg", import.meta.url);

test("recognizes the supplied Fomo proof screenshot", { skip: !existsSync(localProof) }, async () => {
  const proof = await readProof(await readFile(localProof));
  assert.equal(proof.approved, true);
  assert.equal(proof.referrer, "@orangie");
  assert.match(proof.fomoEmail, /privaterelay\.appleid\.com$/);
});

test("recognizes the supplied Fomo mobile profile", { skip: !existsSync(mobileProof) }, async () => {
  const proof = await readProof(await readFile(mobileProof));
  assert.equal(proof.approved, true);
  assert.equal(proof.proofType, "mobile-profile");
});

test("signed access tokens expire and reject tampering", () => {
  const key = "test-secret";
  const token = issueToken("magic", "member@example.com", 60, 1_000, key);
  assert.equal(readToken(token, "magic", 2_000, key).email, "member@example.com");
  assert.equal(readToken(token, "magic", 62_000, key), null);
  assert.equal(readToken(`${token}x`, "magic", 2_000, key), null);
});

test("the protected site swaps the public snapshot URL for the authenticated feed", async () => {
  const source = await readFile(new URL("../web/index.html", import.meta.url), "utf8");
  assert.match(source, /fetch\("\/api\/editorial\?action=snapshot", \{ cache: "no-store" \}\)/);
});
