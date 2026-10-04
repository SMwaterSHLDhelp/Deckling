import { callable, toaster } from "@decky/api";
import type {
  AppState,
  ContextSettings,
  HearingSettings,
  WebSettings,
  NowPlaying,
  OkResult,
  ProviderInput,
  PublicProvider,
  SessionSummary,
  ChatSettings,
  VoiceSettings,
} from "./types";

type SessionResult = OkResult & {
  current_session_id?: string;
  messages?: AppState["messages"];
  sessions?: SessionSummary[];
  provider_id?: string;
  model?: string;
  remember_model?: boolean;
  focused?: boolean;
  chats?: ChatSettings;
};

const CALL_MS = 15000;

// Decky's websocket has no timeout of its own. This fires when the Python
// process never opened its socket (it exited during import) or never replied.
export const BACKEND_DOWN = "Backend not responding.";
export const BACKEND_DOWN_DETAIL =
  "Deckling's Python process did not answer. If it wrote a file before exiting, it is ~/homebrew/logs/Deckling/boot-error.txt or ~/Deckling-diagnostics.txt. The loader log is ~/homebrew/logs/ or journalctl -u plugin_loader.";

type BannerHost = { __decklingBanner?: (message: string) => void };

export const logClient = callable<[message: string], OkResult>("log_client");

export function subscribeFailures(listener: (message: string) => void): () => void {
  const host = window as unknown as BannerHost;
  host.__decklingBanner = listener;
  return () => {
    if (host.__decklingBanner === listener) {
      host.__decklingBanner = undefined;
    }
  };
}

export function reportCallFailure(message: string): void {
  const text = message.trim() || "Deckling could not complete that action.";
  toaster.toast({ title: "Deckling", body: text, duration: 5000 });
  (window as unknown as BannerHost).__decklingBanner?.(text);
  void Promise.race([
    logClient(text).then(() => undefined),
    new Promise<void>((resolve) => {
      window.setTimeout(resolve, 2000);
    }),
  ]).catch(() => undefined);
}

function failedResult(result: unknown): result is OkResult {
  return result !== null && typeof result === "object" && "ok" in result && (result as OkResult).ok === false;
}

function deckyCall<A extends unknown[], R>(name: string, quiet = false): (...args: A) => Promise<R> {
  const fn = callable<A, R>(name);
  return async (...args: A) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const result = await Promise.race([
        fn(...args),
        new Promise<R>((_resolve, reject) => {
          timer = setTimeout(() => {
            reject(new Error(BACKEND_DOWN));
          }, CALL_MS);
        }),
      ]);
      if (!quiet && failedResult(result)) {
        reportCallFailure(result.error || `${name} failed`);
      }
      return result;
    } catch (err) {
      const message = err instanceof Error && err.message ? err.message : `${name} failed`;
      if (!quiet) {
        reportCallFailure(message);
      }
      return { ok: false, error: message } as R;
    } finally {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    }
  };
}

export const getHealth = deckyCall<[], OkResult & { version?: string; traceback?: string }>("health", true);
export const getDiagnostics = deckyCall<[], OkResult & { version?: string; lines?: string[] }>("diagnostics");
export const writeDiagnostics = deckyCall<[], OkResult & { path?: string }>("write_diagnostics");
export const getState = deckyCall<[], AppState & OkResult>("get_state", true);
export const saveProvider = deckyCall<[provider: ProviderInput], OkResult & { provider?: PublicProvider }>(
  "save_provider",
);
export const deleteProvider = deckyCall<[providerId: string], OkResult>("delete_provider");
export const saveSettings = deckyCall<
  [settings: { system_prompt: string; default_provider_id: string; default_model: string }],
  OkResult
>("save_settings");
export const newSession = deckyCall<[], SessionResult>("new_session");
export const switchSession = deckyCall<[sessionId: string], SessionResult>("switch_session");
export const clearSession = deckyCall<[], SessionResult>("clear_session");
export const deleteSession = deckyCall<[sessionId: string], SessionResult>("delete_session");
export const renameSession = deckyCall<[sessionId: string, title: string], SessionResult>("rename_session");
export const pinSession = deckyCall<[sessionId: string, pinned: boolean], SessionResult>("pin_session");
export const moveSession = deckyCall<[sessionId: string, gameKey: string, gameLabel: string], SessionResult>(
  "move_session",
);
export const saveChats = deckyCall<[settings: Partial<ChatSettings>], SessionResult>("save_chats");
export const testProvider = deckyCall<
  [providerId: string],
  OkResult & { message?: string; models?: string[]; vision_models?: string[]; status?: number; latency_ms?: number }
>("test_provider");
export const listModels = deckyCall<[providerId: string], OkResult & { models?: string[]; vision_models?: string[] }>(
  "list_models",
);
export const setModelVision = deckyCall<
  [providerId: string, model: string, enabled: boolean],
  OkResult & { provider?: PublicProvider }
>("set_model_vision");
export const sendMessage = deckyCall<
  [providerId: string, model: string, content: string, requestId: string, aboutGame: string],
  OkResult & { messages?: AppState["messages"]; sessions?: SessionSummary[] }
>("send_message");
export const cancelChat = deckyCall<[requestId: string], OkResult>("cancel_chat");
export const saveVoice = deckyCall<[settings: Partial<VoiceSettings>], OkResult & { voice?: VoiceSettings }>("save_voice");
export const setGameContext = deckyCall<
  [snapshot: Record<string, unknown>],
  SessionResult & { game?: NowPlaying | null; suggestions?: string[]; context?: ContextSettings; notice?: string }
>("set_game_context");
export const saveContext = deckyCall<
  [settings: Partial<ContextSettings>],
  OkResult & { context?: ContextSettings; game?: NowPlaying | null; suggestions?: string[] }
>("save_context");
export const saveWeb = deckyCall<[settings: Record<string, unknown>], OkResult & { web?: WebSettings }>("save_web");
export const testWeb = deckyCall<
  [query?: string],
  OkResult & {
    query?: string;
    backend?: string;
    results?: { title: string; url: string; snippet?: string }[];
    excerpt?: string;
  }
>("test_web");
export const testScreen = deckyCall<[], OkResult & { image_b64?: string; bytes?: number }>("test_screen");
export const saveHearing = deckyCall<[settings: Partial<HearingSettings>], OkResult & { hearing?: HearingSettings }>(
  "save_hearing",
);
export const pushToTalk = deckyCall<[], OkResult & { hearing?: HearingSettings }>("push_to_talk");
export const stopListening = deckyCall<[], OkResult & { hearing?: HearingSettings }>("stop_listening");
export const setHearingActivity = deckyCall<
  [gameRunning: boolean, sleeping: boolean],
  OkResult & { hearing?: HearingSettings }
>("set_hearing_activity");
export const testVoice = deckyCall<[], OkResult & { voice?: VoiceSettings; warning?: string }>("test_voice");
export const stopSpeaking = deckyCall<[], OkResult>("stop_speaking");
export const retryKitten = deckyCall<[], OkResult & { voice?: VoiceSettings }>("retry_kitten");
export const saveLastScreenshot = deckyCall<[], OkResult & { path?: string }>("save_last_screenshot");
export const lookAtScreen = deckyCall<
  [providerId: string, model: string, question: string, requestId: string, game: string, imageB64: string, qamHidden: boolean],
  OkResult & { messages?: AppState["messages"]; sessions?: SessionSummary[]; suggestions?: string[]; vision?: boolean }
>("look_at_screen");
export const startOAuth = deckyCall<
  [providerId: string, flow: string],
  OkResult & { status?: string; message?: string; user_code?: string; verification_url?: string }
>("start_oauth");
export const cancelOAuth = deckyCall<[providerId: string], OkResult>("cancel_oauth");
export const oauthStatus = deckyCall<
  [providerId: string],
  OkResult & { status?: string; message?: string; user_code?: string; verification_url?: string }
>("oauth_status");
