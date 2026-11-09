import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const dataUrl = (code) => `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
const cache = new Map();
async function compile(path) {
  if (cache.has(path)) return cache.get(path);
  const source = readFileSync(path, "utf8");
  let code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const refs = new Map();
  for (const [, name] of code.matchAll(/from ["']([^"']+)["']/g)) {
    if (name === "../store") refs.set(name, dataUrl("export const useStore = (select) => select({agents:[]});"));
    else if (name.startsWith(".")) {
      const base = resolve(dirname(path), name);
      refs.set(name, await compile([base + ".ts", base + ".tsx"].find(existsSync)));
    } else refs.set(name, import.meta.resolve(name));
  }
  code = code.replace(/from ["']([^"']+)["']/g, (_, name) => `from ${JSON.stringify(refs.get(name))}`);
  const url = dataUrl(code); cache.set(path, url); return url;
}
const source = (path) => fileURLToPath(new URL(path, import.meta.url));
const { appendStreamEvent, finishStreamRounds } = await import(await compile(source("../src/lib/researchStream.ts")));
const { researchProgressParts } = await import(await compile(source("../src/lib/researchProgress.ts")));
const { default: ModelRound } = await import(await compile(source("../src/components/ModelRound.tsx")));
const event = (type, delta, round_id = "call:1") => ({ type, delta, round_id, round: 1, agent: "router" });
const render = (part, pending = true) => renderToStaticMarkup(React.createElement(ModelRound, {part, pending, showBadge:false}));

test("interleaved reasoning updates a single stable round without splitting Markdown", () => {
  let parts = appendStreamEvent([], event("thinking", "先核对"));
  assert.match(render(parts[0]), /先核对/);
  assert.match(render(parts[0]), /aria-expanded="true"/);
  parts = appendStreamEvent(parts, event("token", "**结"));
  const identity = parts[0].roundId;
  parts = appendStreamEvent(parts, event("thinking", "再核对\n下一步"));
  assert.equal(parts[0].text, "**结");
  assert.equal(parts[0].reasoning, "先核对再核对\n下一步");
  assert.equal(parts[0].roundId, identity);
  parts = appendStreamEvent(parts, event("token", "论**：完成\n\n| 项 | 值 |\n|---|---|\n| A | 1 |"));
  assert.equal(parts.length, 1);
  const html = render(parts[0]);
  assert.equal((html.match(/思考过程<\/span>/g) ?? []).length, 1);
  assert.match(html, /<strong[^>]*>结论<\/strong>/);
  assert.match(html, /<table/);
  assert.match(html, /animate-blink/);
  parts = appendStreamEvent(parts, {type:"model_round",round_id:"call:1",status:"completed"});
  assert.doesNotMatch(render(parts[0]), /animate-blink|animate-ping/);
});

test("tool boundaries and repeated agent invocations keep their own channels", () => {
  let parts = appendStreamEvent([], event("thinking", "第一轮"));
  parts = [...parts, {type:"tool_call",id:"tool",skill:"date",status:"done"}];
  parts = appendStreamEvent(parts, event("token", "第二轮正文", "call:2"));
  parts = appendStreamEvent(parts, event("thinking", "另一调用", "other-call:1"));
  assert.deepEqual(parts.map(p => p.roundId ?? p.id), ["call:1","tool","call:2","other-call:1"]);
  const final = finishStreamRounds(parts, "router");
  assert.ok(final.filter(p => p.type === "model_round").every(p => p.status === "interrupted"));
  for (const p of final.filter(p => p.type === "model_round")) assert.doesNotMatch(render(p), /animate-blink|animate-ping/);
});

test("failed, cancelled and completed messages stop unfinished reasoning indicators", () => {
  const part = appendStreamEvent([], event("thinking", "已收到的思考"))[0];
  assert.equal(researchProgressParts([part], true)[0].status, "streaming");
  const stopped = researchProgressParts([part], false)[0];
  assert.equal(stopped.reasoning, "已收到的思考");
  assert.equal(stopped.status, "interrupted");
  assert.doesNotMatch(render(part, false), /animate-blink|animate-ping/);
});

test("providers without reasoning only render the actual body", () => {
  const part = appendStreamEvent([], event("token", "普通回答"))[0];
  const html = render(part);
  assert.match(html, /普通回答/);
  assert.doesNotMatch(html, /思考过程/);
});
