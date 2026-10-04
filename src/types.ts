export interface ProviderKindInfo {
  kind: string;
  label: string;
  description: string;
  default_base_url: string;
  default_model: string;
  auth: string;
  oauth: string;
}

export interface PublicProvider {
  id: string;
  kind: string;
  name: string;
  base_url: string;
  default_model: string;
  max_tokens: number;
  has_api_key: boolean;
  api_key_last4: string;
  oauth_client_id: string;
  has_oauth_secret: boolean;
  oauth_connected: boolean;
  oauth_expires_at: number;
  connection_status: string;
  connection_detail: string;
  vision_override?: Record<string, boolean>;
}

export interface ChatMessage {
  id: string;
  role: string;
  content: string;
  created_at: number;
  sources?: { title: string; url: string }[];
  status?: string;
}

export interface SessionSummary {
  id: string;
  title: string;
  updated_at: number;
  game_key: string;
  game_label: string;
  pinned: boolean;
  preview: string;
  provider_id: string;
  model: string;
}

export interface ChatSettings {
  keep: number;
  remember_model: boolean;
}

export function defaultChats(): ChatSettings {
  return { keep: 40, remember_model: true };
}

export interface VoiceSettings {
  voice_enabled: boolean;
  voice_engine: string;
  piper_voice: string;
  kitten_voice: string;
  voice_speed: number;
  screen_capture: boolean;
  kitten_error: string;
  piper_voices: string[];
  kitten_voices: string[];
}

export interface HearingSettings {
  wake_enabled: boolean;
  sensitivity: number;
  wake_model: string;
  stt_model: string;
  ptt_enabled: boolean;
  battery_saver: boolean;
  debug_audio: boolean;
  done_sound: boolean;
  thinking_tick: boolean;
  thinking_tick_set?: boolean;
  wake_error: string;
  mic_source: string;
  stt_backend: string;
  install_message: string;
  install_progress: number;
  phase: string;
  wake_models: { id: string; label: string }[];
  stt_models: string[];
  idle_note: string;
}

export function defaultHearing(): HearingSettings {
  return {
    wake_enabled: false,
    sensitivity: 0.5,
    wake_model: "hey_jarvis",
    stt_model: "tiny.en",
    ptt_enabled: true,
    battery_saver: false,
    debug_audio: false,
    done_sound: true,
    thinking_tick: true,
    thinking_tick_set: false,
    wake_error: "",
    mic_source: "",
    stt_backend: "",
    install_message: "",
    install_progress: 0,
    phase: "off",
    wake_models: [
      { id: "hey_jarvis", label: "hey jarvis" },
      { id: "alexa", label: "alexa" },
      { id: "hey_mycroft", label: "hey mycroft" },
      { id: "hey_rhasspy", label: "hey rhasspy" },
    ],
    stt_models: ["tiny.en", "base.en"],
    idle_note: "The speech model closes after each line.",
  };
}

export function defaultVoice(): VoiceSettings {
  return {
    voice_enabled: false,
    voice_engine: "piper",
    piper_voice: "en_US-lessac-medium",
    kitten_voice: "Jasper",
    voice_speed: 1,
    screen_capture: true,
    kitten_error: "",
    piper_voices: [
      "en_US-lessac-medium",
      "en_US-amy-medium",
      "en_US-ryan-medium",
      "en_GB-alan-medium",
      "en_GB-jenny_dioco-medium",
    ],
    kitten_voices: ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"],
  };
}

export interface ContextSettings {
  share_game_context: boolean;
  include_achievements: boolean;
  include_playtime: boolean;
}

export interface NowPlaying {
  appid: number;
  name: string;
  rich_presence: string;
  achievements_unlocked: number | null;
  achievements_total: number | null;
  capsule: string;
  emulator: string;
  shortcut: boolean;
  sources: string[];
  game_key?: string;
  game_label?: string;
}

export function defaultContext(): ContextSettings {
  return { share_game_context: true, include_achievements: true, include_playtime: true };
}

export interface WebSettings {
  enabled: boolean;
  provider: string;
  searxng_url: string;
  has_brave_key: boolean;
  has_tavily_key: boolean;
  has_serper_key: boolean;
}

export function defaultWeb(): WebSettings {
  return {
    enabled: true,
    provider: "duckduckgo",
    searxng_url: "",
    has_brave_key: false,
    has_tavily_key: false,
    has_serper_key: false,
  };
}

export interface AppState {
  catalog: ProviderKindInfo[];
  providers: PublicProvider[];
  default_provider_id: string;
  default_model: string;
  system_prompt: string;
  current_session_id: string;
  sessions: SessionSummary[];
  messages: ChatMessage[];
  voice: VoiceSettings;
  hearing: HearingSettings;
  context: ContextSettings;
  game: NowPlaying | null;
  suggestions: string[];
  web: WebSettings;
  chats: ChatSettings;
}

export interface ProviderInput {
  id?: string;
  kind: string;
  name: string;
  base_url: string;
  default_model: string;
  max_tokens: number;
  api_key: string | null;
  oauth_client_id: string;
  oauth_client_secret: string | null;
}

export type BackendEvent = {
  type: string;
  request_id?: string;
  session_id?: string;
  text?: string;
  error?: string;
  cancelled?: boolean;
  messages?: ChatMessage[];
  sessions?: SessionSummary[];
  provider_id?: string;
  status?: string;
  message?: string;
  user_code?: string;
  verification_url?: string;
  flow?: string;
  suggestions?: string[];
  vision?: boolean;
  phase?: string;
  transcript?: string;
  action?: string;
  progress?: number;
  score?: number;
};

export interface OkResult {
  ok: boolean;
  error?: string;
}
