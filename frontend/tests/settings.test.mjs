import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

async function loadModule(file) {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
}
const { SETTINGS_VIEWS, settingsRedirect } = await loadModule("../src/lib/settingsRoutes.ts");
const { readTaskDraft, saveTaskDraft, clearTaskDraft } = await loadModule("../src/lib/taskDrafts.ts");

test("all four settings routes have distinct views and the index redirects", () => {
  assert.equal(new Set(Object.entries(SETTINGS_VIEWS).filter(([path]) => path !== "/settings").map(([, view]) => view)).size, 4);
  assert.equal(settingsRedirect("/settings/", ""), "/settings/models");
});
test("legacy profile and platform links redirect, including browser-back navigation", () => {
  assert.equal(settingsRedirect("/backtest/model-lab", "?tab=profiles"), "/settings/models");
  assert.equal(settingsRedirect("/backtest/model-lab/", "?tab=platform"), "/settings/defaults");
});
test("evaluation creation and results links stay in the evaluation workspace", () => {
  for (const search of ["", "?tab=compose", "?tab=results", "?batch=123"]) assert.equal(settingsRedirect("/backtest/model-lab", search), null);
  assert.equal(settingsRedirect("/backtest/event", "?tab=profiles"), null);
});
test("settings round trip retains task inputs while dropping all credentials", () => {
  const entries = new Map();
  globalThis.sessionStorage = { getItem: (key) => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, value), removeItem: (key) => entries.delete(key) };
  const task = { name: "行情研究", members: ["macro", "quant"], date_from: "2026-01-01", profileId: "p1", service: { connection_id: "s1", api_key: "never-persist", headers: { Authorization: "Bearer hidden" }, secret_env_ref: "hidden" } };
  saveTaskDraft("draft", task);
  const restored = readTaskDraft("draft");
  assert.deepEqual(restored, { name: task.name, members: task.members, date_from: task.date_from, profileId: "p1", service: { connection_id: "s1" } });
  assert.doesNotMatch(entries.get("draft"), /never-persist|Bearer|hidden/);
  clearTaskDraft("draft"); assert.equal(readTaskDraft("draft"), null);
});
test("unavailable storage and corrupt drafts do not break task forms", () => {
  globalThis.sessionStorage = { getItem: () => "{bad-json", setItem: () => { throw new Error("storage blocked"); }, removeItem: () => { throw new Error("storage blocked"); } };
  assert.equal(readTaskDraft("draft"), null);
  assert.doesNotThrow(() => saveTaskDraft("draft", { name: "test" }));
  assert.doesNotThrow(() => clearTaskDraft("draft"));
});
