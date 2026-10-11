// esbuild settings for Live's Extension Host: one CommonJS file, evaluated
// in a `vm` context that has `require` for node: builtins and `globalThis`,
// but no `global`.
export function buildOptions({ outfile = "dist/extension.js", externalSdk = false } = {}) {
  return {
    entryPoints: [new URL("./src/extension.ts", import.meta.url).pathname],
    outfile,
    bundle: true,
    platform: "node",
    format: "cjs",
    target: "node20",
    define: { global: "globalThis" },
    external: externalSdk ? ["@ableton-extensions/sdk"] : [],
    logLevel: "info",
  };
}
