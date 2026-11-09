import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const memory = new Map();
globalThis.localStorage = globalThis.sessionStorage = { getItem: (k) => memory.get(k) ?? null, setItem: (k,v) => memory.set(k,v) };
globalThis.window = { location: { pathname: "/", search: "", hostname: "localhost" }, addEventListener() {}, dispatchEvent() {}, history: { pushState(_s,_t,path) { window.location.pathname = path; }, replaceState(_s,_t,path) { window.location.pathname = path; } }, setTimeout(fn, ms) { const timer = setTimeout(fn,ms); timer.unref(); return timer; }, setInterval(fn,ms) { const timer = setInterval(fn,ms); timer.unref(); return timer; } };
globalThis.document = { visibilityState: "visible" };
let stream, finish, cancelled = [];
const details = { B: { case: { id: "B", title: "历史 B" }, messages: [{id:"b",role:"assistant",content:"历史正文"}], artifacts: [{id:"ba",created_at:"2026-01-01"}] } };
globalThis.researchHarness = {
  api: { cases: async () => [{id:"A"},{id:"B"}], caseDetail: async (id) => details[id], chatStatus: async () => ({runs:[]}), cancelChat: async (caseId, runId) => { cancelled.push({caseId,runId}); return {ok:true,cancelled:true}; } },
  async streamChat(_body, handlers) {
    stream = handlers.onEvent;
    await new Promise((resolve,reject) => { finish = resolve; handlers.signal.addEventListener("abort", () => reject(new HarnessAbort())); });
  },
};
class HarnessAbort extends Error {}
const dataUrl = (code) => `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
function compile(path) {
  return ts.transpileModule(readFileSync(new URL(path,import.meta.url),"utf8"), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
}
const mocks = {
  "./api": dataUrl('export const api=globalThis.researchHarness.api; export const streamChat=globalThis.researchHarness.streamChat; export async function startChatDetached(){}; export const StreamAbortedError=globalThis.researchAbort;'),
  "./settingsClient": dataUrl('export async function settingsRequest(){return globalThis.researchHarness.configuration ? await globalThis.researchHarness.configuration() : {model:{ready:true}}}; export function settingsChanged(){};'),
  "./utils": dataUrl('let n=0; export function uid(){return "client-"+(++n)}'),
  "./lib/settingsRoutes": dataUrl(compile("../src/lib/settingsRoutes.ts")),
  "./lib/researchSession": dataUrl(compile("../src/lib/researchSession.ts")),
  "./lib/researchStream": dataUrl(compile("../src/lib/researchStream.ts")),
};
globalThis.researchAbort = HarnessAbort;
const code = compile("../src/store.ts").replace(/from ["']([^"']+)["']/g,(_,name)=>`from ${JSON.stringify(mocks[name] || import.meta.resolve(name))}`);
let useStore, partsFromHistory;
try { ({useStore, partsFromHistory} = await import(dataUrl(code))); }
catch(error) { throw new Error(error.message); }
const nextTick = () => new Promise((r)=>setImmediate(r));

test("navigation and history browsing preserve the original stream without cross-case writes", async () => {
  useStore.setState({currentCaseId:"A",messages:[],artifacts:[],cases:[{id:"A"},{id:"B"}]});
  const pending = useStore.getState().sendMessage("研究 A"); await nextTick();
  stream({type:"meta",case_id:"A",message_id:"server-a",run_id:"run-a"});
  stream({type:"token",agent:"router",delta:"第一段"});
  useStore.getState().setView("settings-services");
  assert.equal(useStore.getState().streaming,true); assert.equal(cancelled.length,0);
  await useStore.getState().loadCase("B");
  stream({type:"token",agent:"router",delta:"第二段"});
  stream({type:"artifact",artifact:{id:"aa",created_at:"2026-01-02"}});
  assert.equal(useStore.getState().currentCaseId,"B");
  assert.equal(useStore.getState().messages[0].content,"历史正文");
  assert.equal(useStore.getState().artifacts[0].id,"ba");
  await useStore.getState().loadCase("A");
  assert.equal(useStore.getState().messages.at(-1).parts[0].text,"第一段第二段");
  await useStore.getState().loadCase("B");
  stream({type:"done",case_id:"A",message_id:"server-a"}); finish(); await pending;
  assert.equal(useStore.getState().currentCaseId,"B");
  assert.equal(useStore.getState().view,"settings-services");
  assert.equal(useStore.getState().messages[0].content,"历史正文");
});
test("explicit stop cancels the original server run, never navigation", async () => {
  useStore.setState({currentCaseId:"A",messages:[],artifacts:[]});
  const pending = useStore.getState().sendMessage("停止测试"); await nextTick();
  stream({type:"meta",case_id:"A",message_id:"server-a2",run_id:"run-a2"});
  useStore.getState().openArenaDetail("arena-test");
  assert.equal(cancelled.length,0); assert.equal(useStore.getState().streaming,true);
  useStore.getState().stop(); await nextTick(); await pending;
  assert.deepEqual(cancelled,[{caseId:"A",runId:"run-a2"}]);
  assert.equal(useStore.getState().streaming,false);
});

test("a slow preflight cannot move a submitted question into another case", async () => {
  let release;
  researchHarness.configuration = () => new Promise((resolve) => { release = resolve; });
  useStore.setState({currentCaseId:"A",messages:[],artifacts:[]});
  const pending = useStore.getState().sendMessage("属于 A 的问题");
  await useStore.getState().loadCase("B");
  release({model:{ready:true}}); await nextTick();
  stream({type:"meta",case_id:"A",message_id:"server-a3",run_id:"run-a3"});
  stream({type:"token",delta:"A 的答案"});
  assert.equal(useStore.getState().activeResearch.caseId,"A");
  assert.equal(useStore.getState().messages[0].content,"历史正文");
  stream({type:"done",case_id:"A",message_id:"server-a3"}); finish(); await pending;
  assert.equal(useStore.getState().currentCaseId,"B");
  delete researchHarness.configuration;
});

test("thinking and body survive navigation and new history replay without duplication", async () => {
  useStore.setState({currentCaseId:"A",messages:[],artifacts:[]});
  const pending = useStore.getState().sendMessage("思考恢复测试"); await nextTick();
  stream({type:"meta",case_id:"A",message_id:"server-thinking",run_id:"run-thinking"});
  stream({type:"thinking",agent:"router",round_id:"invocation:1",round:1,delta:"先检查"});
  stream({type:"token",agent:"router",round_id:"invocation:1",round:1,delta:"**结"});
  useStore.getState().setView("settings-models");
  await useStore.getState().loadCase("B");
  stream({type:"thinking",agent:"router",round_id:"invocation:1",round:1,delta:"再核对"});
  stream({type:"token",agent:"router",round_id:"invocation:1",round:1,delta:"论**"});
  assert.equal(useStore.getState().messages[0].content,"历史正文");
  await useStore.getState().loadCase("A");
  assert.equal(useStore.getState().messages.at(-1).parts.length,1);
  assert.equal(useStore.getState().messages.at(-1).parts[0].reasoning,"先检查再核对");
  stream({type:"model_round",agent:"router",round_id:"invocation:1",status:"completed"});
  const live = useStore.getState().messages.at(-1).parts;
  const restored = partsFromHistory({id:"server-thinking",role:"assistant",content:"**结论**",tool_trace:[
    {type:"stream_layout",version:1},
    {type:"model_round",agent:"router",round_id:"invocation:1",round:1,reasoning:"先检查再核对",text:"**结论**",status:"completed"},
  ]});
  assert.deepEqual(restored,live);
  stream({type:"done",case_id:"A",message_id:"server-thinking"}); finish(); await pending;
  assert.equal(useStore.getState().messages.at(-1).pending,false);
  const legacy = partsFromHistory({id:"old",role:"assistant",content:"旧正文",tool_trace:[{type:"thinking",agent:"router",text:"旧思考"}]});
  assert.deepEqual(legacy.map(p=>p.text),["旧思考","旧正文"]);
});
