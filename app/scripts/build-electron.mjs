// Bundles the main process and the preload script. The preload of a sandboxed renderer
// must be a single CommonJS file without Node imports beyond "electron".
import { build } from "esbuild";

const common = { bundle: true, platform: "node", target: "node22", format: "cjs", external: ["electron"], sourcemap: false };
await build({ ...common, entryPoints: ["electron/main.ts"], outfile: "dist/electron/main.js" });
await build({ ...common, entryPoints: ["electron/preload.ts"], outfile: "dist/electron/preload.js" });
console.log("electron bundles written to dist/electron");
