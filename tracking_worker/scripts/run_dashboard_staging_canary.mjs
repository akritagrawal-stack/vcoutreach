#!/usr/bin/env node

// Narrow remote check for the staging-only dashboard fallback. It reads the
// operator credential from the macOS login Keychain and prints a content-free
// receipt only. It never prints a secret, cookie, URL query, event ID, or body.
import { execFileSync } from "node:child_process";
import { canonicalJson, sha256, validateHttpsOrigin } from "../src/logic.mjs";

const origin = validateHttpsOrigin(process.env.TRACKING_WORKER_URL);
const secret = process.env.STAGING_DASHBOARD_ACCESS_SECRET || execFileSync("/usr/bin/security", ["find-generic-password", "-s", "outreach-signal.tracking", "-a", "dashboard-access-secret", "-w"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
if (secret.length < 32) throw new Error("dashboard keychain secret is unavailable");

async function request(path, options = {}) {
  return fetch(`${origin}${path}`, { redirect: "manual", ...options });
}

async function check(label, response, status) {
  if (response.status !== status) throw new Error(`${label} expected ${status}, received ${response.status}`);
  return { label, status };
}

const checks = [];
checks.push(await check("unauthorized", await request("/api/dashboard"), 401));
checks.push(await check("wrong_bearer", await request("/api/dashboard", { headers: { authorization: "Bearer invalid" } }), 401));
const authorized = await request("/api/dashboard", { headers: { authorization: `Bearer ${secret}` } });
checks.push(await check("authorized_bearer", authorized, 200));
if (authorized.headers.get("access-control-allow-origin") !== null) throw new Error("dashboard CORS is unexpectedly enabled");
const aggregate = await authorized.json();
if (JSON.stringify(aggregate).includes(secret) || aggregate.classification !== "aggregate_only" || aggregate.interpretation?.opens_and_clicks_are_not_confirmed_people !== true) throw new Error("dashboard aggregate contract failed");
checks.push({ label: "aggregate_contract", status: 200, aggregate_only: true, no_cors: true, bot_aware_interpretation: true });
const login = await request("/dashboard/login", {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded", origin },
  body: new URLSearchParams({ secret }),
});
checks.push(await check("login", login, 303));
const cookie = login.headers.get("set-cookie");
if (!cookie || !/^__Host-publik_dashboard=/u.test(cookie) || !/Max-Age=900; HttpOnly; Secure; SameSite=Strict$/u.test(cookie) || cookie.includes(secret)) throw new Error("dashboard cookie contract failed");
checks.push(await check("session", await request("/dashboard", { headers: { cookie: cookie.split(";", 1)[0] } }), 200));
checks.push(await check("logout", await request("/dashboard/logout", { method: "POST", headers: { cookie: cookie.split(";", 1)[0] } }), 204));
checks.push(await check("cross_origin_login", await request("/dashboard/login", {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded", origin: "https://attacker.example.test" },
  body: new URLSearchParams({ secret }),
}), 404));

const receipt = {
  schema_version: "1",
  receipt_kind: "publik_dashboard_staging_canary",
  outcome: "success",
  checks,
  event_security_preserved: "verified_by_full_tracking_canary",
  values_redacted: true,
};
receipt.receipt_hash = await sha256(canonicalJson(receipt));
process.stdout.write(`${JSON.stringify(receipt)}\n`);
