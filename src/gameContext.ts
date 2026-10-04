/** Read the running game from Steam. Missing client methods are skipped. */

export interface GameSnapshot {
  appid: number;
  name: string;
  shortcut: boolean;
  exe: string;
  launch_options: string;
  playtime_minutes: number | null;
  session_started: number | null;
  last_played: number | null;
  rich_presence: string;
  achievements_unlocked: number | null;
  achievements_total: number | null;
  recent_achievements: string[];
  next_achievement: string;
  compat_tool: string;
  recent_screenshot: boolean;
  sources: string[];
}

type Bag = Record<string, unknown>;

const SHORTCUT_TYPE = 1073741824;
const sessionStart = new Map<number, number>();

export function emptySnapshot(): GameSnapshot {
  return {
    appid: 0,
    name: "",
    shortcut: false,
    exe: "",
    launch_options: "",
    playtime_minutes: null,
    session_started: null,
    last_played: null,
    rich_presence: "",
    achievements_unlocked: null,
    achievements_total: null,
    recent_achievements: [],
    next_achievement: "",
    compat_tool: "",
    recent_screenshot: false,
    sources: [],
  };
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function numberOf(value: unknown): number | null {
  const number = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  if (!Number.isFinite(number) || number < 0) {
    return null;
  }
  return Math.floor(number);
}

function firstText(app: Bag, keys: string[]): string {
  for (const key of keys) {
    const value = textOf(app[key]);
    if (value) {
      return value;
    }
  }
  return "";
}

function presenceText(value: unknown): string {
  if (typeof value === "string") {
    return value.trim().slice(0, 160);
  }
  if (!value || typeof value !== "object") {
    return "";
  }
  const record = value as Bag;
  for (const key of ["status", "steam_display", "richPresence", "localized"]) {
    const text = textOf(record[key]);
    if (text) {
      return text.slice(0, 160);
    }
  }
  const parts = Object.values(record)
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .slice(0, 3);
  return parts.join(" · ").slice(0, 160);
}

function achievementName(item: Bag): string {
  return firstText(item, ["name", "strName", "displayName", "apiName", "strID"]);
}

export function achievementsFrom(value: unknown): {
  unlocked: number | null;
  total: number | null;
  recent: string[];
  next: string;
} {
  if (Array.isArray(value)) {
    const rows = value.filter((item): item is Bag => Boolean(item) && typeof item === "object");
    const unlocked = rows.filter((item) => Boolean(item.achieved || item.unlocked || item.bAchieved));
    const locked = rows.filter((item) => !item.achieved && !item.unlocked && !item.bAchieved);
    return {
      unlocked: unlocked.length,
      total: rows.length,
      recent: unlocked
        .slice(-3)
        .map(achievementName)
        .filter(Boolean),
      next: locked[0] ? achievementName(locked[0]) : "",
    };
  }
  if (value && typeof value === "object") {
    const record = value as Bag;
    const recent = Array.isArray(record.recent) ? record.recent.map((item) => textOf(item)).filter(Boolean) : [];
    return {
      unlocked: numberOf(record.unlocked ?? record.nAchieved ?? record.unlockedCount),
      total: numberOf(record.total ?? record.nTotal ?? record.totalCount),
      recent: recent.slice(0, 3),
      next: textOf(record.next || record.nextName),
    };
  }
  return { unlocked: null, total: null, recent: [], next: "" };
}

export function collectGame(
  input: {
    app?: Bag | null;
    richPresence?: unknown;
    achievements?: unknown;
    compat?: unknown;
    recentScreenshot?: boolean;
    now?: number;
  },
): GameSnapshot {
  const app = input.app || {};
  const appid = numberOf(app.appid ?? app.unAppID ?? app.nAppId) ?? 0;
  const appType = numberOf(app.app_type ?? app.appType);
  const shortcut = Boolean(app.shortcut || app.BIsShortcut || appType === SHORTCUT_TYPE || appid >= 2_000_000_000);
  const name = firstText(app, ["display_name", "strDisplayName", "app_name", "name"]);
  const playtime = numberOf(app.minutes_playtime_forever ?? app.nPlaytimeForever ?? app.minutes_played);
  const lastPlayed = numberOf(app.rt_last_time_played ?? app.rtLastPlayed ?? app.last_played);
  const achievements = achievementsFrom(input.achievements);
  const compat =
    textOf(input.compat) ||
    (input.compat && typeof input.compat === "object"
      ? firstText(input.compat as Bag, ["name", "strToolName", "version"])
      : "") ||
    firstText(app, ["compat_tool_name", "selected_compat_tool"]);
  const now = input.now ?? Date.now();
  let started: number | null = null;
  if (appid && name) {
    if (!sessionStart.has(appid)) {
      sessionStart.set(appid, Math.floor(now / 1000));
    }
    started = sessionStart.get(appid) ?? null;
  }
  const sources = ["router"];
  const rich = presenceText(input.richPresence);
  if (rich) {
    sources.push("rich-presence");
  }
  if (achievements.total) {
    sources.push("achievements");
  }
  if (compat) {
    sources.push("compat");
  }
  if (input.recentScreenshot) {
    sources.push("screenshot");
  }
  return {
    appid,
    name,
    shortcut,
    exe: firstText(app, ["strShortcutExe", "executable", "strExePath", "exe"]),
    launch_options: firstText(app, ["strShortcutLaunchOptions", "launch_options", "strLaunchOptions"]),
    playtime_minutes: playtime,
    session_started: started,
    last_played: lastPlayed,
    rich_presence: rich,
    achievements_unlocked: achievements.unlocked,
    achievements_total: achievements.total,
    recent_achievements: achievements.recent,
    next_achievement: achievements.next,
    compat_tool: compat,
    recent_screenshot: Boolean(input.recentScreenshot),
    sources,
  };
}

async function tryCall(owner: Bag | undefined, names: string[], args: unknown[]): Promise<unknown> {
  if (!owner) {
    return undefined;
  }
  for (const name of names) {
    const fn = owner[name];
    if (typeof fn !== "function") {
      continue;
    }
    try {
      const result = (fn as (...values: unknown[]) => unknown).apply(owner, args);
      if (result && typeof (result as Promise<unknown>).then === "function") {
        return await Promise.race([
          result as Promise<unknown>,
          new Promise((resolve) => {
            window.setTimeout(() => resolve(undefined), 400);
          }),
        ]);
      }
      return result;
    } catch {
      // This Steam build uses a different method name.
    }
  }
  return undefined;
}

export interface LifetimeNote {
  unAppID?: number;
  nAppID?: number;
  appid?: number;
  bRunning?: boolean;
}

export interface FocusTracker {
  runningIds: number[];
  sawLifetime: boolean;
  lastMainId: number;
}

export interface GameSources {
  main: Bag | null;
  running: Bag[];
}

export interface SteamLike {
  GameSessions?: {
    RegisterForAppLifetimeNotifications?: (callback: (note: LifetimeNote) => void) => { unregister?: () => void };
  };
  Apps?: Bag;
}

const RUNNING = 4;
const LAUNCHING = 1;

export function emptyFocus(): FocusTracker {
  return { runningIds: [], sawLifetime: false, lastMainId: 0 };
}

export function appidOf(app: Bag | null | undefined): number {
  if (!app) {
    return 0;
  }
  return numberOf(app.appid ?? app.unAppID ?? app.nAppId) ?? 0;
}

function lastPlayed(app: Bag): number {
  return numberOf(app.rt_last_time_played ?? app.rtLastPlayed ?? app.last_played) ?? 0;
}

function isRunningApp(app: Bag): boolean {
  const status = numberOf(app.display_status);
  if (status === null) {
    return true;
  }
  return status === RUNNING || status === LAUNCHING;
}

export function noteLifetime(state: FocusTracker, note: LifetimeNote): FocusTracker {
  const id = numberOf(note.unAppID ?? note.appid ?? note.nAppID) ?? 0;
  if (!id) {
    return { ...state, sawLifetime: true };
  }
  const rest = state.runningIds.filter((item) => item !== id);
  const runningIds = note.bRunning === false ? rest : [id, ...rest];
  return { runningIds, sawLifetime: true, lastMainId: state.lastMainId };
}

export function noteMain(state: FocusTracker, app: Bag | null): FocusTracker {
  const id = appidOf(app);
  if (!id || id === state.lastMainId) {
    return state;
  }
  // The router can still name the previous game the first time we see it.
  // A lifetime notification that already arrived is the focused app.
  if (state.sawLifetime && state.lastMainId === 0) {
    return { ...state, lastMainId: id };
  }
  const rest = state.runningIds.filter((item) => item !== id);
  return { runningIds: [id, ...rest], sawLifetime: state.sawLifetime, lastMainId: id };
}

export function resolveFocused(
  state: FocusTracker,
  sources: { main?: Bag | null; running?: Bag[] | null; byId?: (id: number) => Bag | null },
): Bag | null {
  const running = (sources.running || []).filter((item) => item && isRunningApp(item));
  if (state.sawLifetime) {
    const id = state.runningIds[0] || 0;
    if (!id) {
      return null;
    }
    const fromLookup = sources.byId?.(id) || null;
    if (fromLookup) {
      return fromLookup;
    }
    const fromList = running.find((item) => appidOf(item) === id) || null;
    if (fromList) {
      return fromList;
    }
    if (sources.main && appidOf(sources.main) === id) {
      return sources.main;
    }
    return { appid: id };
  }
  const main = sources.main || null;
  if (main && appidOf(main)) {
    return running.find((item) => appidOf(item) === appidOf(main)) || main;
  }
  if (running.length === 0) {
    return null;
  }
  if (running.length === 1) {
    return running[0];
  }
  return [...running].sort((left, right) => lastPlayed(right) - lastPlayed(left))[0];
}

function overviewFrom(steam: SteamLike | undefined, id: number): Bag | null {
  const apps = steam?.Apps;
  if (!apps) {
    return null;
  }
  for (const name of ["GetAppOverviewByAppID", "GetAppOverviewByGameID"]) {
    const fn: unknown = apps[name];
    if (typeof fn !== "function") {
      continue;
    }
    try {
      const result = (fn as (appid: number) => unknown).call(apps, id);
      if (result && typeof result === "object") {
        return result as Bag;
      }
    } catch {
      // This Steam build uses a different method name.
    }
  }
  return null;
}

export function createGameWatch(options: {
  steam?: SteamLike;
  sources: () => GameSources;
  onFocus?: (app: Bag | null) => void;
}) {
  let state = emptyFocus();
  const cache = new Map<number, Bag>();
  let lastSig = "";
  let stopped = false;

  const remember = (app: Bag | null | undefined) => {
    const id = appidOf(app);
    if (!id || !app) {
      return;
    }
    if (firstText(app, ["display_name", "strDisplayName", "app_name", "name"])) {
      cache.set(id, app);
    }
  };

  const byId = (id: number): Bag | null => {
    const fetched = overviewFrom(options.steam, id);
    if (fetched && firstText(fetched, ["display_name", "strDisplayName", "app_name", "name"])) {
      remember(fetched);
      return fetched;
    }
    return cache.get(id) || fetched;
  };

  const focusedFrom = (sources: GameSources): Bag | null => {
    remember(sources.main);
    for (const app of sources.running) {
      remember(app);
    }
    return resolveFocused(state, { main: sources.main, running: sources.running, byId });
  };

  const publish = (app: Bag | null) => {
    const sig = app ? `${appidOf(app)}|${firstText(app, ["display_name", "strDisplayName", "app_name", "name"])}` : "";
    if (sig === lastSig) {
      return;
    }
    lastSig = sig;
    options.onFocus?.(app);
  };

  const onNote = (note: LifetimeNote) => {
    if (stopped) {
      return;
    }
    const id = numberOf(note.unAppID ?? note.appid ?? note.nAppID) ?? 0;
    if (id && note.bRunning !== false) {
      remember(byId(id));
    }
    state = noteLifetime(state, note);
    publish(focusedFrom(options.sources()));
  };

  let unsubscribe = () => undefined as void;
  const sessions = options.steam?.GameSessions;
  const register = sessions?.RegisterForAppLifetimeNotifications;
  if (sessions && typeof register === "function") {
    try {
      const handle = register.call(sessions, onNote);
      unsubscribe = () => {
        handle?.unregister?.();
      };
    } catch {
      unsubscribe = () => undefined;
    }
  }

  return {
    stop() {
      stopped = true;
      unsubscribe();
    },
    refresh(): Bag | null {
      const sources = options.sources();
      state = noteMain(state, sources.main);
      const app = focusedFrom(sources);
      publish(app);
      return app;
    },
    peek(): Bag | null {
      return focusedFrom(options.sources());
    },
  };
}

type LiveRouter = { MainRunningApp?: Bag | null; RunningApps?: Bag[] };
let deckyRouter: LiveRouter | null = null;
let liveWatch: ReturnType<typeof createGameWatch> | null = null;
let lastSnapshot: GameSnapshot | null = null;

export async function primeRouter(): Promise<void> {
  try {
    deckyRouter = (await import("@decky/ui")).Router as LiveRouter;
  } catch {
    deckyRouter = null;
  }
}

export function syncSources(): GameSources {
  const ui = (window as unknown as { SteamUIStore?: LiveRouter }).SteamUIStore;
  const storeMain = (ui?.MainRunningApp || null) as Bag | null;
  const deckyMain = (deckyRouter?.MainRunningApp || null) as Bag | null;
  const storeRunning = Array.isArray(ui?.RunningApps) ? ui.RunningApps : [];
  const deckyRunning = Array.isArray(deckyRouter?.RunningApps) ? deckyRouter.RunningApps : [];
  return {
    main: storeMain || deckyMain,
    running: storeRunning.length ? storeRunning : deckyRunning,
  };
}

export function bindGameWatch(watch: ReturnType<typeof createGameWatch> | null): void {
  liveWatch = watch;
}

export function peekFocusedName(): { known: boolean; name: string } {
  if (!liveWatch) {
    return { known: false, name: "" };
  }
  const app = liveWatch.peek();
  if (!app) {
    return { known: true, name: "" };
  }
  return { known: true, name: firstText(app, ["display_name", "strDisplayName", "app_name", "name"]) };
}

export function watchLiveGame(onFocus: (app: Bag | null) => void): ReturnType<typeof createGameWatch> {
  const steam = (window as unknown as { SteamClient?: SteamLike }).SteamClient;
  const watch = createGameWatch({ steam, sources: syncSources, onFocus });
  bindGameWatch(watch);
  return watch;
}

export async function readLiveGame(now = Date.now()): Promise<GameSnapshot> {
  try {
    await primeRouter();
    const steam = (window as unknown as { SteamClient?: Bag }).SteamClient;
    const app = (liveWatch ? liveWatch.refresh() : syncSources().main) as Bag | null;
    if (!app) {
      lastSnapshot = emptySnapshot();
      return lastSnapshot;
    }
    const appid = numberOf(app.appid ?? app.unAppID ?? app.nAppId) ?? 0;
    const apps = steam?.Apps as Bag | undefined;
    const [richPresence, achievements, compat, screenshot] = await Promise.all([
      tryCall(apps, ["GetRichPresence", "GetAppRichPresence"], [appid]),
      tryCall(apps, ["GetMyAchievementsForApp", "GetAchievements", "GetAchievementProgress"], [appid]),
      tryCall(apps, ["GetCompatToolInfo", "GetCompatTool"], [appid]),
      tryCall(steam?.Screenshots as Bag | undefined, ["GetLastScreenshot", "GetRecentScreenshot"], [appid]),
    ]);
    const snapshot = collectGame({
      app,
      richPresence,
      achievements,
      compat,
      recentScreenshot: Boolean(screenshot),
      now,
    });
    if (!snapshot.name && lastSnapshot && lastSnapshot.appid === snapshot.appid && lastSnapshot.name) {
      return lastSnapshot;
    }
    lastSnapshot = snapshot.name ? snapshot : emptySnapshot();
    return snapshot.name ? snapshot : emptySnapshot();
  } catch {
    return lastSnapshot || emptySnapshot();
  }
}
