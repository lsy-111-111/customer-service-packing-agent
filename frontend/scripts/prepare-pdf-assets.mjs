import { cpSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = dirname(fileURLToPath(import.meta.resolve("pdfjs-dist/package.json")));
const outputRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../public/pdfjs");
mkdirSync(outputRoot, { recursive: true });
for (const folder of ["cmaps", "standard_fonts", "wasm", "iccs"]) {
  cpSync(resolve(packageRoot, folder), resolve(outputRoot, folder), { recursive: true });
}
