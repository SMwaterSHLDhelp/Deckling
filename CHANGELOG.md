# Changelog

All notable changes to Deckling are recorded here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Move items from **Unreleased** into a version section before tagging. The release workflow copies that section into the GitHub Release notes. A tag that contains a hyphen, such as `v0.1.0-rc.4`, is published as a prerelease and does not replace `/releases/latest/`.

## [Unreleased]

## [0.1.0-rc.18] - 2026-10-04

### Fixed

- Screen help works more than once. The first look kept the in-flight request open for the whole vision stream, and the next "what am I looking at" (or Look at my screen) returned without starting a capture and without an error. A new look cancels that request and starts again. Steam's screenshot call gives up after a few seconds and the backend capture still runs. Each shot gets its own file, a file left from the previous shot is not read again, and a gamescopectl or grim process that hangs is killed. For llama.cpp and other local servers, older screenshots are removed from the request and replaced with "[earlier screenshot]", so only the new picture is sent. A failed capture or vision call is shown on screen and in a toast.
- Soft tick while thinking is on for a new install, and for an existing install that never used that switch. Turning the switch off keeps it off.

## [0.1.0-rc.17] - 2026-10-04

### Added

- A Steam toast reads "Taking photo" whenever a screenshot is taken: the Look at my screen button, a voice phrase such as "how do I do this", Steam+Y, or the model calling look_at_screen. A second toast says "Looking at your screen...". The toast uses Decky's toaster, so it shows while a game is running and while the Quick Access menu is closed. Vision models get a look_at_screen tool only when Screen help's capture switch is on. The JPEG is attached on the next turn. If capture is off, the tool is not offered and nothing is captured.
- Spoken replies stay words. While spoken replies are on, the system prompt asks for one to three plain sentences, with no markdown, lists, or symbols, unless more detail was requested. Before Piper or KittenTTS, markdown, emoji, and other symbols are removed, lists become sentences, abbreviations such as "e.g." are expanded, a code block becomes "I put the code in the chat.", and a URL becomes "The link is in the chat." The message in the chat stays formatted. The whole sanitized reply is spoken.
- While a reply is being spoken, the chat shows Stop talking. "stop", "stop talking", "shut up", and "be quiet" stop speech. The wake word stays on during playback, and other words heard while the voice is playing are ignored so the reply does not answer itself. Pressing the push-to-talk chord during playback stops speech instead of starting a new recording. Playback ends immediately, later sentences are not started, and the text reply stays on screen.

## [0.1.0-rc.16] - 2026-10-03

### Fixed

- Web lookup treats HTTP 202 and any page with no results as a miss, then tries the next source. The order is DuckDuckGo HTML (browser GET), DuckDuckGo lite, then Bing HTML. A 202 is retried once with the cookies from that response and a short random pause. Test web lookup names the backend that answered. If every keyless source fails, the error says to add a SearXNG URL or a Brave or Tavily key.
- After you send a message, a thinking bubble appears immediately, with a typing indicator, a status line (Thinking, Searching the web, Reading pages, Looking at your screen, Writing), and an elapsed time after 3 seconds. The reply still streams into that bubble. Reasoning tokens stay on the Thinking line instead of being written as the answer. Stop stays on screen. A soft tick while thinking is in Voice settings and is off by default.
- Screen capture runs as the deck user when Decky is root, with `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `GAMESCOPE_WAYLAND_DISPLAY`, and `DISPLAY`. It tries `gamescopectl screenshot`, grim, the gamescope control socket, PipeWire, then a recent Steam shot under `/home/deck`. A failed Steam path falls through to that chain. The error on screen includes which step failed. Screen help has Test screen capture, which shows a thumbnail or the error. llama.cpp at the Watercrest host accepts an OpenAI image part and described a solid red test JPEG as red.

## [0.1.0-rc.15] - 2026-10-03

### Fixed

- Spoken replies play. Piper's Linux archive contains library symlinks (`libpiper_phonemize.so` and the rest). The extractor treated every symlink as unsafe, so install stopped with "Archive path is not safe" and nothing was spoken. In-tree links are kept. A link that points outside the plugin folder is still rejected. Playback uses the deck user's PipeWire session (`XDG_RUNTIME_DIR`, `PULSE_SERVER`, `PIPEWIRE_RUNTIME_DIR`) via paplay, pw-cat, pw-play, or aplay, and switches to the deck user when Decky is root. Piper gets its own library directory on `LD_LIBRARY_PATH`.
- Web lookup runs when the model decides to search. llama.cpp streams `tool_calls` in pieces, and Qwen also writes `<tool_call>` in the reply or in `reasoning_content`. Those calls are collected and executed. If the model says it will look something up and never emits a call, Deckling searches and asks again with the excerpts. DuckDuckGo is queried with a browser POST to the HTML endpoint, then a GET if that returns HTTP 202, then lite.duckduckgo.com. A failed search is not cached, and the error is returned to the model. Privacy and Web has Test web lookup, which shows titles, URLs, a short excerpt, or the error.

### Added

- A lower, descending tone plays when listening stops (end of speech, silence, or push-to-talk release), on the same path as the wake-word ding. Voice settings include Sound when done listening, on by default.

## [0.1.0-rc.14] - 2026-10-03

### Fixed

- Wake word install no longer uses SteamOS Python. `/usr/bin/python3` has no pip, and the root filesystem is read-only, which produced "No module named pip". The first time listening is turned on, Deckling downloads python-build-standalone 3.11 (x86_64 or aarch64), checks its SHA-256, and installs openWakeWord and the speech wheels into a virtualenv under the plugin data folder. Install progress is saved, and a whisper.cpp failure is shown instead of leaving "Installing whisper.cpp" on screen.
- llama.cpp models that can see images are recognized. Detection reads `capabilities` containing `multimodal` or `vision`, llama.cpp `/props` `modalities.vision`, and the model name. A "This model can see images" switch on the provider always wins.
- The Quick Access menu is the chat: messages, the ask field, the microphone, Look at my screen, the model switcher, and Settings. Everything else is a full-screen page with Steam's SidebarNavigation. Tabs are Providers, Voice, Spoken replies, Screen help, Privacy and Web, Chats, and Advanced. Shoulder buttons or the D-pad move between tabs, and B goes back. Each tab is short, with longer notes behind a row.

## [0.1.0-rc.13] - 2026-10-03

### Fixed

- HTTPS no longer depends on PluginLoader's OpenSSL directory (`/usr/lib/ssl`). That path is missing on SteamOS, so `http.client` raised `SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]`. llama.cpp treated every `OSError` as a LAN firewall problem, which is the "Can't reach llama-server" line. Requests now use a vendored certifi bundle, then `/etc/ssl/certs/ca-certificates.crt` or `/etc/ca-certificates/extracted/tls-ca-bundle.pem`. Verification stays on. A public host shows the real exception. The firewall hint is only for a LAN address or a refused connection. Test connection shows the HTTP status and how long it took. Save still works when the model list fails.
- Saying the wake word can no longer kill the backend. The wake and speech workers were started with `sys.executable`, which inside Decky is the PluginLoader binary, not Python. That starts a second loader. Post-wake recording and transcription now run in a separate process, using the system Python. A failure is caught, shown in a toast and in Diagnostics, and the worker is started again. The plugin process keeps answering.

## [0.1.0-rc.12] - 2026-10-03

### Fixed

- The backend starts on Decky Loader 3.2.9. That loader's Python does not include `http.server` (or `socketserver`, `glob`, and `tty`). rc.11 only replaced modules missing from 3.2.6, so importing `http.server` still exited the process before it could answer, and settings kept saying the backend was not responding.
- If importing the backend still fails, the process stays up. `health` returns the traceback, and it is also written to ~/homebrew/logs/Deckling/boot-error.txt and ~/Deckling-diagnostics.txt.
- A dead backend is one short line, with the traceback behind Details. The same message is not repeated under Problem or as a toast. Back is a plain row, not a white text field.

## [0.1.0-rc.11] - 2026-10-03

### Fixed

- Backend calls no longer all time out on Decky Loader. The loader's Python is a PyInstaller build that does not include `pty`, `wave`, `html.parser`, or `urllib.robotparser`. Importing those at startup made the plugin process exit before it could answer, so health and Save provider reported a timeout. Those modules are now bundled and loaded only when the interpreter does not already have them.
- If startup still fails, the traceback is written to ~/Deckling-diagnostics.txt and to the plugin log folder, and health returns the error instead of the process exiting. A call that gets no answer says "Backend not responding" and points at ~/homebrew/logs/Deckling/. Decky's Developer tab shows the UI console, not that Python log.

## [0.1.0-rc.10] - 2026-10-03

### Fixed

- Settings rows respond to the A button and to touch. Save provider, the wake word switch, and the spoken-reply engine were only listening for a click, so the gamepad did nothing and a failed backend call left the row unchanged.
- A preset such as llama.cpp opens its form directly. The type is already chosen. URL, key, and name are first, and Save stays in the dialog footer. Change type is a single row in that same dialog.
- Spoken replies show the voice, speed, and test line only for the engine you picked. Wake word sensitivity and the wake word list appear only after openWakeWord is on. Sign-in fields and search keys appear only for the choice that uses them.
- The top of settings shows whether the backend is connected. Advanced, Diagnostics shows the last log lines, and can copy them or write them to ~/Deckling-diagnostics.txt. A failed call shows the reason on screen.
- A missing speech package can no longer stop the plugin from starting. Those imports stay inside the worker that needs them.

## [0.1.0-rc.9] - 2026-10-03

### Fixed

- Save provider works with a name, a type, and a key or URL. The model can stay blank. After a successful save, the model list loads in the same dialog.
- The settings page uses Steam's settings layout. Provider dialogs scroll, and Save stays in the dialog footer. The Steam action bar stays at the bottom of the screen instead of covering the provider list, so those buttons receive the A button and touch.
- A failed save or model list shows the reason and writes it to the plugin log.

## [0.1.0-rc.8] - 2026-10-03

### Added

- Chats are grouped by the running game. A Steam game uses its app id. A shortcut or ROM uses that title. When nothing is running, chats stay in General. Each game can have several chats.
- Opening Quick Access while a game is running shows that game's most recent chat. New chat starts another one for the same game. The chat list shows the current game first, with a preview and the time it was updated. You can open, rename, pin, move, or delete a chat. New chats are named from the first question.
- Each chat can remember the provider and model you used. Advanced has that switch, and a cap of 20, 40, or 80 chats. Older unpinned chats are removed. Existing conversations are migrated into one file per chat.

## [0.1.0-rc.7] - 2026-10-03

### Added

- The running game is read from Steam and added to each reply as a short Game context block: name, rich presence, session time, and achievement progress when those fields are available. Non-Steam shortcuts use the shortcut name, executable, and launch options, and a ROM title is taken from the command line when it is there.
- Public store details (genres, a short description, and the developer) are cached per app id for a week. Guides and a PCGamingWiki search link are included when the app id is a real Steam game.
- A Now playing card at the top of the chat shows the capsule, name, rich presence line, and achievement count. Suggested prompts follow the game. Privacy has Share game context with AI, on by default, plus switches to leave achievements or playtime out of the prompt.
- Web lookup, on by default, can search and read public pages about the current game. DuckDuckGo is the default search, with SearXNG or a Brave, Tavily, or Serper key as alternatives. Models that accept tools can call web_search and fetch_page. Other models get a short excerpt in the prompt. Pages are cached, rate-limited, and checked against robots.txt. Sources show under the answer.

## [0.1.0-rc.6] - 2026-10-03

### Changed

- The Quick Access chat is a short column: the provider and model are one button, replies render lists and code, and Stop generation stays next to the message field while a reply is streaming. Look at my screen, New chat, and Summarize are chips. The microphone button shows listening, transcribing, and speaking.
- Settings are grouped into Providers, Voice, Spoken replies, Screen help, Privacy, and Advanced. Provider text fields stay in a dialog. The first run offers Ollama, llama.cpp, OpenAI, Gemini, Grok, and Claude, then an optional voice step.
- A failed connection is remembered on the provider card. Chat errors say what to try next.

### Fixed

- A reply that is still streaming no longer lands in a conversation you already switched away from.
- Sending or stopping no longer leaves a failed request spinning when the backend call throws.
- Default model and system prompt are edited in a dialog, so the Steam keyboard is not attached to the long settings page.

## [0.1.0-rc.5] - 2026-10-02

### Added

- Voice control, off until the wake word is enabled. hey jarvis is the default and downloads on first use, along with alexa, hey mycroft, and hey rhasspy. A custom hey deckling model is not included. Sensitivity, push to talk (the Quick Access button and Steam + X), and a pause-during-games switch are in settings. The wake word uses openWakeWord 0.4 on ONNX, because newer releases need `tflite-runtime` and that wheel does not install on SteamOS's Python. Speech recognition uses faster-whisper at int8 (`tiny.en` or `base.en`) and falls back to whisper.cpp when that wheel will not install. Models stay in the plugin data directory. The speech process exits after each line.
- Spoken confirmations. While an answer is waiting for a go-ahead, "go ahead", "yes", and "do it" confirm, and "cancel" or "stop" cancel. "New chat" and "stop listening" work by voice. Screen-help phrases still look at the game.
- Listening pauses while the Deck is asleep. Microphone audio is local and is not saved unless debug audio is on.

## [0.1.0-rc.4] - 2026-10-02

### Fixed

- Provider name, URL, and key open in their own dialog. Typing no longer redraws the settings page, so the Steam keyboard stays on the field you are editing.

### Changed

- The plugin is Deckling. The Quick Access title, settings page, chat labels, toasts, and logs use that name. The install zip and the folder inside it are `Deckling`. Decky treats that folder as a new plugin, so an older install stays in the plugin list until you uninstall it. On first start, credentials and chats are copied from the previous folders when the new files are missing.
- After a Dependabot minor or patch merge, the auto-merge workflow starts the build and CodeQL workflows on `main`.

### Added

- Spoken replies, off until enabled. Piper is the default engine and KittenTTS is the second. Each downloads its voice on first use into the plugin data directory. Speed, a per-engine voice picker, and a test button are in settings. Playback uses the Deck user's PipeWire session and stops when you send, stop, or start a new chat. If KittenTTS cannot install, Piper still works.
- Screen help. Phrases such as "how do I do this" and "what am I looking at", the **Look at my screen** button, and Steam + Y when the controller API exists, capture the game, hide the Quick Access Menu first, and send a JPEG of about 1280px to the selected vision model with the game's name. Text-only models are called out, with a switch to one that can see images. The answer is short and is spoken when voice replies are on. Screenshots are not stored unless you save them, and a setting turns capture off.
- Dependabot updates for npm, the CI Python tools, and GitHub Actions, with minor and patch updates grouped apart from majors.
- A weekly Actions run that rebuilds against the latest `@decky/ui`, `@decky/api`, and Decky CLI, and opens an issue if that build fails.
- CodeQL scanning for JavaScript/TypeScript and Python.
- Mocked contract tests for each chat provider's request and response format.
- Security policy, bug and feature issue forms, and this changelog.

## [0.1.0-rc.3] - 2026-10-02

### Fixed

- Add provider no longer waits on `get_state` to fill the type list. The types ship with the frontend. A failed settings load shows the backend error, including when the plugin is still starting.

### Added

- After a provider is saved, or its URL or key changes, the settings page and the Quick Access chat load that server's models into a button list. A text field and Refresh models stay available.

## [0.1.0-rc.2] - 2026-10-02

### Fixed

- Adding a provider on the Deck. The type choices are buttons on the settings page, because the Steam dropdown does not open reliably there.
- llama.cpp accepts a base URL with or without `/v1`, uses the only loaded model when the model field is blank, and reports loading, API-key, and connection failures in plain language.
- Streaming reads `reasoning_content` when a model sends that instead of `content`.

## [0.1.0-rc.1] - 2026-10-02

### Added

- Decky Loader plugin that chats from the Quick Access Menu with OpenAI, Anthropic, Claude Code subscriptions, xAI Grok, Gemini, Hermes, Ollama, llama.cpp, and custom OpenAI-compatible servers.
- Credentials stored on the Deck at mode `0600`, with secrets redacted from the plugin log.
- GitHub Actions builds `AI-Assistant.zip` and attaches it to version tags.
