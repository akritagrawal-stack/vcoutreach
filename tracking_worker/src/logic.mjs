const ID = /^(?:campaign|action|link)_[0-9a-f]{32}$/;
const P17_TEST_ACTION = /^test_action_[0-9a-f]{24}$/;
const KEY = /^[a-z][a-z0-9_]{2,127}$/;
const UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const SIGNATURE = /^[A-Za-z0-9_-]{43}$/;

export function canonicalJson(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (!value || typeof value !== "object") throw new Error("unsupported canonical value");
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

function text(value) { return new TextEncoder().encode(value); }
function base64url(bytes) { return btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, ""); }

export async function sha256(value) {
  const digest = await crypto.subtle.digest("SHA-256", text(value));
  return `sha256:${Array.from(new Uint8Array(digest), (item) => item.toString(16).padStart(2, "0")).join("")}`;
}

export async function hmac(value, secret) {
  if (typeof secret !== "string" || secret.length < 32) throw new Error("signing secret must be at least 32 characters");
  const key = await crypto.subtle.importKey("raw", text(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return base64url(new Uint8Array(await crypto.subtle.sign("HMAC", key, text(canonicalJson(value)))));
}

export async function constantTimeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string") return false;
  const a = text(left);
  const b = text(right);
  const width = Math.max(a.length, b.length);
  let difference = a.length ^ b.length;
  for (let index = 0; index < width; index += 1) difference |= (a[index % (a.length || 1)] || 0) ^ (b[index % (b.length || 1)] || 0);
  return difference === 0;
}

export function configuredSecret(value) { return typeof value === "string" && value.length >= 32; }

export function isoNow(now = new Date()) { return now.toISOString().replace(/\.\d{3}Z$/u, "Z"); }
export function hourBucket(at) { return `${at.slice(0, 13)}:00:00Z`; }

export function requireOpaqueId(value, prefix) {
  if (typeof value !== "string" || !ID.test(value) || !value.startsWith(`${prefix}_`)) throw new Error(`invalid ${prefix} identifier`);
  return value;
}

export function requireDestinationKey(value) {
  if (typeof value !== "string" || !KEY.test(value)) throw new Error("invalid destination key");
  return value;
}

export function requireUtc(value) {
  if (typeof value !== "string" || !UTC.test(value) || Number.isNaN(Date.parse(value)) || new Date(value).toISOString().replace(/\.\d{3}Z$/u, "Z") !== value) throw new Error("invalid UTC timestamp");
  return value;
}

export function validateHttpsOrigin(value) {
  if (typeof value !== "string" || value !== value.trim()) throw new Error("origin must be a hosted HTTPS origin");
  const url = new URL(value);
  const host = url.hostname.toLowerCase();
  if (url.protocol !== "https:" || (url.pathname !== "/" && url.pathname !== "") || url.search || url.hash || url.username || url.password || (url.port && url.port !== "443") || !host.includes(".") || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || /^\d{1,3}(?:\.\d{1,3}){3}$/u.test(host) || host.includes(":")) throw new Error("origin must be a hosted HTTPS DNS origin");
  return `https://${host}`;
}

export function exactOriginList(value) {
  if (typeof value !== "string" || !value) throw new Error("exact origin allow-list is required");
  const origins = value.split(",").map((item) => validateHttpsOrigin(item.trim()));
  if (!origins.length || new Set(origins).size !== origins.length) throw new Error("origin allow-list is invalid");
  return origins;
}

export function linkClaims(link, origin) {
  // This is deliberately byte-compatible with P39 _link_claims.
  return { action_id: link.action_id, campaign_id: link.campaign_id, destination_key: link.destination_key, expires_at: link.expires_at, link_id: link.link_id, origin };
}

export function unsubscribeClaims(link, origin) {
  return { action_id: link.action_id, expires_at: link.expires_at, link_id: link.link_id, origin, route: "unsubscribe" };
}

export function openClaims(link, origin) {
  return { action_id: link.action_id, campaign_id: link.campaign_id, destination_key: link.destination_key, expires_at: link.expires_at, link_id: link.link_id, origin, route: "open" };
}

export function deckClaims(link, origin) {
  return { action_id: link.action_id, campaign_id: link.campaign_id, destination_key: link.destination_key, expires_at: link.expires_at, link_id: link.link_id, origin, route: "deck" };
}

export async function signedLinkUrl(link, origin, secret) {
  return `${origin}/r/${link.link_id}?s=${await hmac(linkClaims(link, origin), secret)}`;
}

export async function expectedP39LinkHash(link, origin, secret) {
  const signature = await hmac(linkClaims(link, origin), secret);
  const payload = {
    schema_version: "1",
    link_kind: "publik_pre_send_hosted_tracking_link",
    origin,
    campaign_id: link.campaign_id,
    action_id: link.action_id,
    link_id: link.link_id,
    destination_key: link.destination_key,
    expires_at: link.expires_at,
    public_url: `${origin}/r/${link.link_id}?s=${signature}`,
    signature_hash: await sha256(signature),
    authority: { network_accessed: false, tracking_deployed: false, redirect_performed: false, storage_written: false, delivery_authorized: false },
  };
  return sha256(canonicalJson(payload));
}

export async function signedUnsubscribeUrl(link, origin, secret) {
  return `${origin}/u/${link.link_id}?s=${await hmac(unsubscribeClaims(link, origin), secret)}`;
}

export async function signedOpenUrl(link, origin, secret) {
  return `${origin}/o/${link.link_id}?s=${await hmac(openClaims(link, origin), secret)}`;
}

export async function signedDeckUrl(link, origin, secret) {
  return `${origin}/d/${link.link_id}?s=${await hmac(deckClaims(link, origin), secret)}`;
}

export async function verifyRouteSignature(link, origin, signature, route, secret) {
  if (typeof signature !== "string" || !SIGNATURE.test(signature)) return false;
  const claims = route === "unsubscribe" ? unsubscribeClaims(link, origin) : route === "open" ? openClaims(link, origin) : route === "deck" ? deckClaims(link, origin) : linkClaims(link, origin);
  const expected = await hmac(claims, secret);
  return constantTimeEqual(signature, expected);
}

export function exactSignature(url) {
  const values = url.searchParams.getAll("s");
  if (url.searchParams.size !== 1 || values.length !== 1 || !SIGNATURE.test(values[0])) throw new Error("exact signature query is required");
  return values[0];
}

export function classifyRequest({ eventType, occurredAt, acceptedAt, userAgent, cf = {}, recentCount = 0 }) {
  const ua = String(userAgent || "").toLowerCase();
  const organization = String(cf.asOrganization || "").toLowerCase();
  const asn = Number(cf.asn || 0);
  const scanner = /googleimageproxy|ggpht|googleusercontent|yahoomailproxy|proxy\.ymail|outlook\.com|safelinks|proofpoint|mimecast|barracuda|urlscan|linkpreview|slackbot|telegrambot|go-http|java\/|apache-httpclient|python-requests|axios|okhttp|curl|wget|headlesschrome/u.test(ua);
  const gmail = /googleimageproxy|ggpht|googleusercontent/u.test(ua);
  const safeLinks = /safelinks|outlook\.com/u.test(ua);
  const proofpoint = /proofpoint/u.test(ua);
  const browser = /(?:chrome|firefox|safari|edg)\//u.test(ua) && !scanner;
  const securityNetwork = /security|proofpoint|mimecast|barracuda|microsoft/u.test(organization);
  const hostingNetwork = /amazon|aws|google|azure|digitalocean|hetzner|ovh|linode|hosting|cloud|datacenter/u.test(organization) || new Set([16509, 14618, 15169, 396982, 8075, 14061, 60781]).has(asn);
  const mobileNetwork = /mobile|wireless|cellular|t-mobile|verizon|at&t/u.test(organization);
  const residentialNetwork = /comcast|spectrum|cox|charter|frontier|optimum|xfinity/u.test(organization);
  const sourceKind = gmail ? "gmail_proxy" : safeLinks ? "safe_links" : proofpoint ? "proofpoint" : scanner ? "generic_scanner" : browser ? "direct_browser" : "unknown";
  const userAgentFamily = gmail ? "mail_proxy" : safeLinks ? "link_rewriter" : (proofpoint || scanner) ? "security_scanner" : browser ? "browser" : "unknown";
  const networkClass = securityNetwork ? "security" : hostingNetwork ? "hosting" : mobileNetwork ? "mobile" : residentialNetwork ? "residential" : "unknown";
  const machineCadence = recentCount >= 3;
  const fanoutCount = Math.min(50, Math.max(1, recentCount + 1));
  const reasons = [];
  if ((eventType === "click" || eventType === "deck_view") && Date.parse(occurredAt) - Date.parse(acceptedAt) < 180000) reasons.push("post_send_latency_under_180_seconds");
  if (sourceKind === "gmail_proxy") reasons.push("gmail_proxy");
  if (["safe_links", "proofpoint", "generic_scanner"].includes(sourceKind)) reasons.push("security_scanner");
  if (["mail_proxy", "security_scanner", "link_rewriter"].includes(userAgentFamily)) reasons.push("automation_user_agent_family");
  if (["hosting", "security"].includes(networkClass)) reasons.push("automation_network_class");
  if (machineCadence) reasons.push("machine_cadence");
  if (fanoutCount > 1) reasons.push("link_fanout");
  if (reasons.length) return { sourceKind, userAgentFamily, networkClass, machineCadence, fanoutCount, classification: "automation_likely", reasonCodes: [...new Set(reasons)].sort() };
  if (eventType === "open") return { sourceKind, userAgentFamily, networkClass, machineCadence, fanoutCount, classification: "unknown", reasonCodes: ["open_not_person_confirmation"] };
  // A deck view does not require a residential or mobile network class, since
  // most ISPs classify as unknown. It only needs a direct, non-scanner browser
  // that already cleared the automation checks above (latency, scanner UA,
  // hosting or security network, machine cadence, fanout).
  if (eventType === "deck_view") {
    if (sourceKind === "direct_browser" && userAgentFamily === "browser") return { sourceKind, userAgentFamily, networkClass, machineCadence, fanoutCount, classification: "human_likely", reasonCodes: ["plausible_browser_followthrough"] };
    return { sourceKind, userAgentFamily, networkClass, machineCadence, fanoutCount, classification: "unknown", reasonCodes: ["insufficient_human_or_automation_evidence"] };
  }
  if (sourceKind === "direct_browser" && userAgentFamily === "browser" && ["residential", "mobile"].includes(networkClass)) return { sourceKind, userAgentFamily, networkClass, machineCadence, fanoutCount, classification: "human_likely", reasonCodes: ["plausible_browser_followthrough"] };
  return { sourceKind, userAgentFamily, networkClass, machineCadence, fanoutCount, classification: "unknown", reasonCodes: ["insufficient_human_or_automation_evidence"] };
}

export async function validateP18Receipt(receipt, actionIdKind = "action") {
  const required = ["schema_version", "receipt_kind", "action_id", "manifest_hash", "local_execution_receipt_hash", "request_hash", "message_sha256", "message_id_sha256", "outcome", "executed_at", "provider_message_id_hash", "retry_disposition", "automatic_retry_authorized", "network_accessed", "live_recipient_used", "authority", "receipt_hash"];
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || Object.keys(receipt).length !== required.length || required.some((key) => !(key in receipt))) throw new Error("receipt shape is invalid");
  if (actionIdKind === "action") requireOpaqueId(receipt.action_id, "action");
  else if (actionIdKind === "p17_test" && (!P17_TEST_ACTION.test(receipt.action_id))) throw new Error("receipt source action identifier is invalid");
  else if (actionIdKind !== "action" && actionIdKind !== "p17_test") throw new Error("receipt action identifier policy is invalid");
  requireUtc(receipt.executed_at);
  const hash = /^sha256:[a-f0-9]{64}$/u;
  for (const key of ["manifest_hash", "local_execution_receipt_hash", "request_hash", "message_sha256", "message_id_sha256", "receipt_hash"]) if (typeof receipt[key] !== "string" || !hash.test(receipt[key])) throw new Error("receipt hash is invalid");
  if (receipt.provider_message_id_hash !== null && (typeof receipt.provider_message_id_hash !== "string" || !hash.test(receipt.provider_message_id_hash))) throw new Error("provider message hash is invalid");
  const authority = { automation_promotion_authorized: false, arbitrary_recipient_authorized: false, delivery_authorized: true, external_actions_authorized: true, external_use_authorized: false, live_delivery_authorized: false, network_access_authorized: true, provider_send_authorized: true, submission_authorized: false, test_delivery_authorized: true, tracking_authorized: false };
  if (receipt.schema_version !== "1" || receipt.receipt_kind !== "publik_gmail_test_provider_result" || receipt.outcome !== "success" || receipt.retry_disposition !== "no_retry_needed" || receipt.automatic_retry_authorized !== false || receipt.network_accessed !== true || receipt.live_recipient_used !== false || canonicalJson(receipt.authority) !== canonicalJson(authority)) throw new Error("receipt is not a successful P18 test receipt");
  const unsigned = { ...receipt };
  delete unsigned.receipt_hash;
  if (receipt.receipt_hash !== await sha256(canonicalJson(unsigned))) throw new Error("receipt hash does not match content");
  return receipt;
}

export async function validateManualLiveReceipt(receipt) {
  const required = ["schema_version", "receipt_kind", "action_id", "manifest_hash", "local_execution_receipt_hash", "request_hash", "message_sha256", "message_id_sha256", "outcome", "executed_at", "provider_message_id_hash", "retry_disposition", "automatic_retry_authorized", "network_accessed", "live_recipient_used", "authority", "receipt_hash"];
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || Object.keys(receipt).length !== required.length || required.some((key) => !(key in receipt))) throw new Error("live receipt shape is invalid");
  requireOpaqueId(receipt.action_id, "action");
  requireUtc(receipt.executed_at);
  const hash = /^sha256:[a-f0-9]{64}$/u;
  for (const key of ["manifest_hash", "local_execution_receipt_hash", "request_hash", "message_sha256", "message_id_sha256", "receipt_hash"]) if (typeof receipt[key] !== "string" || !hash.test(receipt[key])) throw new Error("live receipt hash is invalid");
  if (typeof receipt.provider_message_id_hash !== "string" || !hash.test(receipt.provider_message_id_hash)) throw new Error("live provider message hash is invalid");
  const authority = { automation_promotion_authorized: false, arbitrary_recipient_authorized: false, delivery_authorized: true, external_actions_authorized: true, external_use_authorized: true, live_delivery_authorized: true, network_access_authorized: true, provider_send_authorized: true, submission_authorized: false, test_delivery_authorized: false, tracking_authorized: true };
  if (receipt.schema_version !== "1" || receipt.receipt_kind !== "publik_gmail_manual_live_provider_result" || receipt.outcome !== "success" || receipt.retry_disposition !== "no_retry_needed" || receipt.automatic_retry_authorized !== false || receipt.network_accessed !== true || receipt.live_recipient_used !== true || canonicalJson(receipt.authority) !== canonicalJson(authority)) throw new Error("receipt is not a successful manual live result");
  const unsigned = { ...receipt };
  delete unsigned.receipt_hash;
  if (receipt.receipt_hash !== await sha256(canonicalJson(unsigned))) throw new Error("live receipt hash does not match content");
  return receipt;
}

const BRIDGE_AUTHORITY = { automation_promotion_authorized: false, delivery_authorized: false, external_actions_authorized: false, external_use_authorized: false, live_delivery_authorized: false, tracking_authorized: false };
const INBOX_AUTHORITY = { delivery_authorized: false, gmail_send_authorized: false, gmail_read_authorized: true, live_delivery_authorized: false };

function hash(value) { return typeof value === "string" && /^sha256:[a-f0-9]{64}$/u.test(value); }

async function trackingActionId(source) {
  const digest = await sha256(canonicalJson({ source_action_id: source.source_action_id, source_inbox_receipt_hash: source.source_inbox_receipt_hash, source_manifest_hash: source.source_manifest_hash, source_provider_receipt_hash: source.source_provider_receipt_hash }));
  return `action_${digest.slice("sha256:".length, "sha256:".length + 32)}`;
}

async function validateP17Manifest(manifest) {
  const required = ["schema_version", "manifest_kind", "action_id", "created_at", "candidate_id", "contact_record_hash", "vault_record_hash", "source_provenance_hash", "verified_at", "expires_at", "prepare_contact_lookup_receipt_hash", "recipient_email_hash", "recipient_person_firm_hash", "test_recipient_email_hash", "research_receipt_hash", "draft_hash", "subject_sha256", "body_sha256", "deck_sha256", "route_type", "suppression_export_sha256", "suppression_records_sha256", "suppression_record_count", "suppression_checks", "execution_mode", "delivery_target", "status", "network_accessed", "live_recipient_authorized", "local_test_execution_authorized", "authority", "manifest_hash"];
  const authority = { automation_promotion_authorized: false, delivery_authorized: false, external_actions_authorized: false, external_use_authorized: false, live_delivery_authorized: false, network_access_authorized: false, submission_authorized: false, tracking_authorized: false };
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest) || Object.keys(manifest).length !== required.length || required.some((key) => !(key in manifest)) || manifest.schema_version !== "1" || manifest.manifest_kind !== "publik_local_test_action" || !P17_TEST_ACTION.test(manifest.action_id) || !KEY.test(manifest.candidate_id) || manifest.execution_mode !== "local_test_only" || manifest.delivery_target !== "approved_test_inbox" || manifest.status !== "ready_for_local_test_execution" || manifest.network_accessed !== false || manifest.live_recipient_authorized !== false || manifest.local_test_execution_authorized !== true || manifest.route_type !== "cold_email" || !hash(manifest.test_recipient_email_hash) || (!Number.isInteger(manifest.suppression_record_count) || manifest.suppression_record_count < 0) || canonicalJson(manifest.authority) !== canonicalJson(authority) || !Array.isArray(manifest.suppression_checks) || canonicalJson(manifest.suppression_checks) !== canonicalJson(["prepare", "pre_execute"]) || !hash(manifest.manifest_hash)) throw new Error("bridge source manifest is invalid");
  for (const key of ["contact_record_hash", "vault_record_hash", "source_provenance_hash", "prepare_contact_lookup_receipt_hash", "recipient_email_hash", "recipient_person_firm_hash", "test_recipient_email_hash", "research_receipt_hash", "draft_hash", "subject_sha256", "body_sha256", "deck_sha256", "suppression_export_sha256", "suppression_records_sha256"]) if (!hash(manifest[key])) throw new Error("bridge source manifest hash is invalid");
  requireUtc(manifest.created_at); requireUtc(manifest.verified_at); requireUtc(manifest.expires_at);
  const unsigned = { ...manifest };
  delete unsigned.manifest_hash;
  if (manifest.manifest_hash !== await sha256(canonicalJson(unsigned))) throw new Error("bridge source manifest hash is invalid");
  return manifest;
}

async function validateInboxReceipt(receipt) {
  const required = ["schema_version", "receipt_kind", "action_id", "manifest_hash", "local_execution_receipt_hash", "provider_result_receipt_hash", "provider_message_id_hash", "gmail_message_id_hash", "message_id_hash", "subject_sha256", "verified_at", "arrival_status", "inbox_arrival_confirmed", "attachment_presence_confirmed", "network_accessed", "gmail_send_performed", "authority", "receipt_hash"];
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || Object.keys(receipt).length !== required.length || required.some((key) => !(key in receipt))) throw new Error("bridge inbox receipt shape is invalid");
  if (!P17_TEST_ACTION.test(receipt.action_id) || receipt.schema_version !== "1" || receipt.receipt_kind !== "publik_gmail_test_inbox_arrival" || receipt.arrival_status !== "confirmed_inbox" || receipt.inbox_arrival_confirmed !== true || receipt.attachment_presence_confirmed !== true || receipt.network_accessed !== true || receipt.gmail_send_performed !== false || canonicalJson(receipt.authority) !== canonicalJson(INBOX_AUTHORITY)) throw new Error("bridge inbox receipt policy is invalid");
  for (const key of ["manifest_hash", "local_execution_receipt_hash", "provider_result_receipt_hash", "provider_message_id_hash", "gmail_message_id_hash", "message_id_hash", "subject_sha256", "receipt_hash"]) if (!hash(receipt[key])) throw new Error("bridge inbox receipt hash is invalid");
  requireUtc(receipt.verified_at);
  const unsigned = { ...receipt };
  delete unsigned.receipt_hash;
  if (receipt.receipt_hash !== await sha256(canonicalJson(unsigned))) throw new Error("bridge inbox receipt hash does not match content");
  return receipt;
}

export async function validateTrackingBridgeActivation(receipt) {
  const required = ["schema_version", "receipt_kind", "action_id", "source_action_id", "source_manifest_hash", "source_manifest", "source_provider_receipt", "source_inbox_receipt", "bridge", "executed_at", "expires_at", "authority", "receipt_hash"];
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt) || Object.keys(receipt).length !== required.length || required.some((key) => !(key in receipt))) throw new Error("bridge activation shape is invalid");
  if (receipt.schema_version !== "1" || receipt.receipt_kind !== "publik_p17_to_p39_tracking_activation" || canonicalJson(receipt.authority) !== canonicalJson(BRIDGE_AUTHORITY) || !hash(receipt.source_manifest_hash) || !hash(receipt.receipt_hash)) throw new Error("bridge activation policy is invalid");
  requireOpaqueId(receipt.action_id, "action"); requireUtc(receipt.executed_at); requireUtc(receipt.expires_at);
  const manifest = await validateP17Manifest(receipt.source_manifest);
  const provider = await validateP18Receipt(receipt.source_provider_receipt, "p17_test");
  const inbox = await validateInboxReceipt(receipt.source_inbox_receipt);
  const source = { source_action_id: provider.action_id, source_manifest_hash: provider.manifest_hash, source_provider_receipt_hash: provider.receipt_hash, source_inbox_receipt_hash: inbox.receipt_hash };
  if (receipt.source_action_id !== source.source_action_id || receipt.source_manifest_hash !== source.source_manifest_hash || manifest.action_id !== source.source_action_id || manifest.manifest_hash !== source.source_manifest_hash || inbox.action_id !== source.source_action_id || inbox.manifest_hash !== source.source_manifest_hash || inbox.local_execution_receipt_hash !== provider.local_execution_receipt_hash || inbox.provider_result_receipt_hash !== provider.receipt_hash || receipt.action_id !== await trackingActionId(source)) throw new Error("bridge activation source binding is invalid");
  const bridge = receipt.bridge;
  const bridgeKeys = ["schema_version", "artifact_kind", "source_action_id", "source_manifest_hash", "source_provider_receipt_hash", "source_inbox_receipt_hash", "tracking_action_id", "provider_accepted_at", "created_at", "expires_at", "authority", "bridge_hash"];
  if (!bridge || typeof bridge !== "object" || Array.isArray(bridge) || Object.keys(bridge).length !== bridgeKeys.length || bridgeKeys.some((key) => !(key in bridge)) || bridge.schema_version !== "1" || bridge.artifact_kind !== "publik_p17_to_p39_tracking_action_bridge" || canonicalJson(bridge.authority) !== canonicalJson(BRIDGE_AUTHORITY) || !hash(bridge.bridge_hash)) throw new Error("bridge artifact policy is invalid");
  for (const key of ["source_manifest_hash", "source_provider_receipt_hash", "source_inbox_receipt_hash"]) if (!hash(bridge[key])) throw new Error("bridge artifact hash is invalid");
  requireUtc(bridge.provider_accepted_at); requireUtc(bridge.created_at); requireUtc(bridge.expires_at);
  if (bridge.source_action_id !== source.source_action_id || bridge.source_manifest_hash !== source.source_manifest_hash || bridge.source_provider_receipt_hash !== source.source_provider_receipt_hash || bridge.source_inbox_receipt_hash !== source.source_inbox_receipt_hash || bridge.tracking_action_id !== receipt.action_id || bridge.provider_accepted_at !== provider.executed_at || bridge.expires_at !== receipt.expires_at || Date.parse(bridge.created_at) < Date.parse(provider.executed_at) || Date.parse(bridge.expires_at) <= Date.parse(bridge.created_at)) throw new Error("bridge artifact binding is invalid");
  const unsignedBridge = { ...bridge }; delete unsignedBridge.bridge_hash;
  if (bridge.bridge_hash !== await sha256(canonicalJson(unsignedBridge))) throw new Error("bridge artifact hash does not match content");
  const unsigned = { ...receipt }; delete unsigned.receipt_hash;
  if (receipt.receipt_hash !== await sha256(canonicalJson(unsigned))) throw new Error("bridge activation hash does not match content");
  return receipt;
}

export async function validateActivationReceipt(receipt) {
  if (receipt && typeof receipt === "object" && !Array.isArray(receipt) && receipt.receipt_kind === "publik_p17_to_p39_tracking_activation") return validateTrackingBridgeActivation(receipt);
  if (receipt && typeof receipt === "object" && !Array.isArray(receipt) && receipt.receipt_kind === "publik_gmail_manual_live_provider_result") return validateManualLiveReceipt(receipt);
  return validateP18Receipt(receipt);
}

export function destinationMap(raw, exactOrigins) {
  let value;
  try { value = JSON.parse(raw); } catch { throw new Error("destination map is invalid JSON"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("destination map is invalid");
  const map = new Map();
  for (const [key, destination] of Object.entries(value)) {
    requireDestinationKey(key);
    const url = new URL(String(destination));
    const origin = validateHttpsOrigin(url.origin);
    if (!exactOrigins.includes(origin) || url.username || url.password || url.search || url.hash) throw new Error("destination is outside exact HTTPS allow-list");
    map.set(key, url.toString());
  }
  return map;
}
