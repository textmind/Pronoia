export const SETTINGS_VIEWS = {
  "/settings": "settings-models",
  "/settings/models": "settings-models",
  "/settings/defaults": "settings-defaults",
  "/settings/services": "settings-services",
  "/settings/simulation": "settings-simulation",
} as const;

export function settingsRedirect(pathname: string, search: string): string | null {
  const path = pathname.replace(/\/+$/, "");
  if (path === "/settings") return "/settings/models";
  if (path !== "/backtest/model-lab") return null;
  const tab = new URLSearchParams(search).get("tab");
  return tab === "profiles" ? "/settings/models" : tab === "platform" ? "/settings/defaults" : null;
}
