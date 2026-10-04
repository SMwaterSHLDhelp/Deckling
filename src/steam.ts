import { toaster } from "@decky/api";
import { Router } from "@decky/ui";
import { peekFocusedName } from "./gameContext";

export function runningGameName(): string {
  const watched = peekFocusedName();
  if (watched.known) {
    return watched.name;
  }
  try {
    const name = Router.MainRunningApp?.display_name?.trim() ?? "";
    return name;
  } catch {
    return "";
  }
}

export async function copyText(text: string): Promise<boolean> {
  const steam = (
    window as unknown as {
      SteamClient?: { System?: { SetClipboardText?: (value: string) => void } };
    }
  ).SteamClient;
  try {
    if (steam?.System?.SetClipboardText) {
      steam.System.SetClipboardText(text);
      toaster.toast({ title: "Deckling", body: "Copied", duration: 2000 });
      return true;
    }
  } catch {
    // Fall through to the browser clipboard.
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      toaster.toast({ title: "Deckling", body: "Copied", duration: 2000 });
      return true;
    }
  } catch {
    // Fall through to the hidden textarea path.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (ok) {
      toaster.toast({ title: "Deckling", body: "Copied", duration: 2000 });
    }
    return ok;
  } catch {
    toaster.toast({ title: "Deckling", body: "Could not copy that reply" });
    return false;
  }
}

export function newRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `req-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}
