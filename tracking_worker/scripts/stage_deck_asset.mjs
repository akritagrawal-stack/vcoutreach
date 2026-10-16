#!/usr/bin/env node

// Stages the real pitch-deck PDF into deck_assets/ for the Workers
// static-assets binding, after verifying the file is actually a PDF and
// matches an owner-supplied SHA-256. It never fetches or guesses a source
// file. Usage:
//   node scripts/stage_deck_asset.mjs <source.pdf> <expected_sha256>
import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PDF_MAGIC = "%PDF-";
const SHA256_HEX = /^[a-f0-9]{64}$/u;

async function main() {
  const [sourcePath, expectedSha256] = process.argv.slice(2);
  if (!sourcePath || !expectedSha256) {
    console.error("usage: node scripts/stage_deck_asset.mjs <source.pdf> <expected_sha256>");
    process.exitCode = 1;
    return;
  }
  const normalizedExpected = expectedSha256.trim().toLowerCase();
  if (!SHA256_HEX.test(normalizedExpected)) {
    console.error("expected sha256 must be exactly 64 lowercase hex characters");
    process.exitCode = 1;
    return;
  }
  let buffer;
  try {
    buffer = await readFile(sourcePath);
  } catch (error) {
    console.error(`could not read source file: ${error instanceof Error ? error.message : error}`);
    process.exitCode = 1;
    return;
  }
  if (buffer.subarray(0, PDF_MAGIC.length).toString("latin1") !== PDF_MAGIC) {
    console.error("source file does not start with the %PDF- magic bytes");
    process.exitCode = 1;
    return;
  }
  const actualSha256 = createHash("sha256").update(buffer).digest("hex");
  if (actualSha256 !== normalizedExpected) {
    console.error(`sha256 mismatch: expected ${normalizedExpected}, got ${actualSha256}`);
    process.exitCode = 1;
    return;
  }
  const workerRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const destinationDir = path.join(workerRoot, "deck_assets");
  const destinationPath = path.join(destinationDir, "deck.pdf");
  await mkdir(destinationDir, { recursive: true });
  await copyFile(sourcePath, destinationPath);
  console.log(`staged deck asset: ${destinationPath} (sha256:${actualSha256})`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
});
