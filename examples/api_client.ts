// Call the docket HTTP API from TypeScript (Node 18+, Deno, Bun, or the browser).
// Run: npx tsx examples/api_client.ts invoice.pdf receipt.jpg
// Typed clients can be generated from docs/openapi.json, e.g.
//   npx openapi-typescript docs/openapi.json -o docket-api.d.ts
import { readFile } from "node:fs/promises";
import { basename } from "node:path";

const API = process.env.DOCKET_URL ?? "http://localhost:8000";
const headers: Record<string, string> = process.env.DOCKET_API_KEY
  ? { Authorization: `Bearer ${process.env.DOCKET_API_KEY}` }
  : {};

async function check(response: Response) {
  if (!response.ok) {
    const { error } = await response.json(); // always {"error": {"code", "message"}}
    throw new Error(`${response.status} ${error.code}: ${error.message}`);
  }
  return response;
}

async function processBatch(paths: string[]) {
  const form = new FormData();
  for (const path of paths) form.append("files", new Blob([await readFile(path)]), basename(path));
  const job = await (await check(await fetch(`${API}/jobs`, { method: "POST", headers, body: form }))).json();

  for (;;) {
    const status = await (await check(await fetch(`${API}/jobs/${job.job_id}`, { headers }))).json();
    if (status.status === "completed" || status.status === "failed") break;
    await new Promise((r) => setTimeout(r, 2000));
  }
  const jsonl = await (await check(await fetch(`${API}/jobs/${job.job_id}/results.jsonl`, { headers }))).text();
  return jsonl.trim().split("\n").map((line) => JSON.parse(line));
}

for (const result of await processBatch(process.argv.slice(2))) {
  console.log(result.source, result.status, result.document_type, result.extracted);
  // Each field's page region: result.field_sources[field].bbox = {x0, y0, x1, y1} in 0..1.
  if (result.needs_review) console.warn("  needs review:", result.review_reasons);
}
