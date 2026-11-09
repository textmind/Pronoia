// Persist task inputs only; connection credentials belong to the server.
export function readTaskDraft<T>(key: string): T | null {
  try { const saved = sessionStorage.getItem(key); return saved ? JSON.parse(saved) as T : null; }
  catch { return null; }
}
export function saveTaskDraft(key: string, value: unknown) {
  try { sessionStorage.setItem(key, JSON.stringify(value, (field, item) => /api.?key|secret|authorization|headers/i.test(field) ? undefined : item)); }
  catch { /* Storage can be unavailable in private sessions. */ }
}
export function clearTaskDraft(key: string) {
  try { sessionStorage.removeItem(key); } catch { /* Storage unavailable. */ }
}
