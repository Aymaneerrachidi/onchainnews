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

const localProof = new URL("../new/fomo.webp", import.meta.url);

test("recognizes the supplied Fomo proof screenshot", { skip: !existsSync(localProof) }, async () => {
  const proof = await readProof(await readFile(localProof));
  assert.equal(proof.approved, true);
  assert.equal(proof.referrer, "@orangie");
  assert.match(proof.fomoEmail, /privaterelay\.appleid\.com$/);
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
  assert.match(source, /fetch\("data\/latest\.json", \{ cache: "no-store" \}\)/);
});
