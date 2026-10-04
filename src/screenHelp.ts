/** Order the Quick Access panel follows before the backend capture. */
export const CAPTURE_STEPS = ["hide-qam", "wait", "steam-screenshot", "backend-capture"] as const;

export const QAM_HIDE_MS = 380;
export const STEAM_SHOT_MS = 2500;

/** Kept in sync with py_modules/ai_assistant/vision.py SCREEN_PHRASES. */
export const SCREEN_PHRASES = [
  "how do i do this",
  "what am i looking at",
  "help me with this",
  "what should i do here",
  "what's on my screen",
  "what is on my screen",
  "what's this",
  "what is this",
  "look at my screen",
  "look at the screen",
  "what should i do",
] as const;

type SteamRecord = Record<string, unknown>;

type SteamClient = {
  Screenshots?: SteamRecord;
  GameSessions?: SteamRecord;
  Input?: SteamRecord;
  Controller?: SteamRecord;
};

export function wantsScreenLook(text: string): boolean {
  const cleaned = text
    .toLowerCase()
    .replace(/[^a-z0-9' ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return SCREEN_PHRASES.some((phrase) => cleaned.includes(phrase));
}

export async function prepareScreenCapture(
  hideMenus: () => void,
  wait: (ms: number) => Promise<void>,
  steamShot: () => Promise<string | null>,
): Promise<string | null> {
  hideMenus();
  await wait(QAM_HIDE_MS);
  return withTimeout(steamShot(), STEAM_SHOT_MS);
}

function withTimeout(work: Promise<string | null>, ms: number): Promise<string | null> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), ms);
    work.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      () => {
        clearTimeout(timer);
        resolve(null);
      },
    );
  });
}

export async function trySteamScreenshot(): Promise<string | null> {
  const steam = steamClient();
  const calls: Array<[SteamRecord | undefined, string]> = [
    [steam?.Screenshots, "TakeScreenshot"],
    [steam?.Screenshots, "RequestScreenshot"],
    [steam?.GameSessions, "TakeScreenshot"],
  ];
  for (const [owner, name] of calls) {
    if (!owner) {
      continue;
    }
    const fn = owner[name];
    if (typeof fn !== "function") {
      continue;
    }
    try {
      const value = await Promise.resolve((fn as (this: SteamRecord) => unknown).call(owner));
      const text = screenshotText(value);
      if (text) {
        return text;
      }
    } catch {
      // Steam's screenshot names are not stable. The backend capture still runs.
    }
  }
  return null;
}

/** Steam + Y when the Input API exists. The Quick Access button does not need it. */
export function bindScreenChord(onFire: () => void): () => void {
  const steam = steamClient();
  const owners = [steam?.Input, steam?.Controller];
  for (const owner of owners) {
    if (!owner) {
      continue;
    }
    const register = owner.RegisterForControllerAction || owner.RegisterForControllerChord;
    if (typeof register !== "function") {
      continue;
    }
    try {
      const token = (register as (this: SteamRecord, spec: unknown, cb: () => void) => unknown).call(
        owner,
        { buttons: ["guide", "face_y"], description: "Look at my screen" },
        onFire,
      );
      return () => {
        const unregister = owner.UnregisterForControllerAction || owner.UnregisterForControllerChord;
        if (typeof unregister === "function") {
          try {
            (unregister as (this: SteamRecord, value: unknown) => void).call(owner, token);
          } catch {
            // The chord registration is already gone.
          }
        }
      };
    } catch {
      // This Steam build does not expose that registration call.
    }
  }
  return () => {};
}

function steamClient(): SteamClient | undefined {
  return (window as unknown as { SteamClient?: SteamClient }).SteamClient;
}

function screenshotText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (!value || typeof value !== "object") {
    return null;
  }
  const record = value as Record<string, unknown>;
  for (const key of ["data", "base64", "image", "path"]) {
    const item = record[key];
    if (typeof item === "string" && item.trim()) {
      return item.trim();
    }
  }
  return null;
}
