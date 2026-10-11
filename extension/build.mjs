// Bundles src/extension.ts into dist/extension.js (see build-options.mjs).
//
//   npm run build          needs the SDK: npm install --no-save <path>/ableton-extensions-sdk-….tgz
//   npm run build:check    leaves the SDK out, to check that our own code bundles
import { build } from "esbuild";
import { createRequire } from "node:module";

import { buildOptions } from "./build-options.mjs";

const externalSdk = process.argv.includes("--external-sdk");
if (!externalSdk) {
  try {
    createRequire(import.meta.url).resolve("@ableton-extensions/sdk");
  } catch {
    console.error("The Extensions SDK is not installed. It comes with Live's beta program:\n"
      + "  npm install --no-save <path>/ableton-extensions-sdk-1.0.0-beta.N.tgz\n"
      + "then run npm run build again.");
    process.exit(1);
  }
}
await build(buildOptions({ externalSdk }));
