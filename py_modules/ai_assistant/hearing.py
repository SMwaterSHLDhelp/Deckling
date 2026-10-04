"""On-device wake word and speech to text. Models download into the plugin data dir, never the zip.

openWakeWord runs in its own low-priority process. Speech recognition prefers a
faster-whisper install in that same data directory and falls back to a
whisper.cpp binary when the wheels will not install on SteamOS.
"""

from __future__ import annotations

import array
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import wave
from collections.abc import Callable
from typing import Any

from .audio_in import capture_command, deck_audio_env, list_input_sources, preferred_source
from .diagnostics import remember
from .http_util import USER_AGENT
from .interpreter import frozen_runtime
from .runtime_python import ensure_runtime_python, ensure_voice_venv
from .store import Store, normalize_hearing
from .vad import MAX_MS, NO_SPEECH_MS, RATE, THRESHOLD, rms, speech_region
from .vision import wants_screen_look
from .voice import playback_command, safe_extract

WAKE_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
WAKE_MODELS = (
    {"id": "hey_jarvis", "file": "hey_jarvis_v0.1.onnx", "label": "hey jarvis"},
    {"id": "alexa", "file": "alexa_v0.1.onnx", "label": "alexa"},
    {"id": "hey_mycroft", "file": "hey_mycroft_v0.1.onnx", "label": "hey mycroft"},
    {"id": "hey_rhasspy", "file": "hey_rhasspy_v0.1.onnx", "label": "hey rhasspy"},
)
FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx")
STT_MODELS = ("tiny.en", "base.en")
WHISPER_RELEASE = "https://github.com/ggml-org/whisper.cpp/releases/download/b4938"
WHISPER_ARCHIVES = {
    "x86_64": f"{WHISPER_RELEASE}/whisper-bin-ubuntu-x64.tar.gz",
    "aarch64": f"{WHISPER_RELEASE}/whisper-bin-ubuntu-arm64.tar.gz",
    "arm64": f"{WHISPER_RELEASE}/whisper-bin-ubuntu-arm64.tar.gz",
}
GGML_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model}.bin"
FASTER_PACKAGES = ("faster-whisper",)
# 0.5+ requires tflite-runtime on Linux, and that wheel stops at Python 3.11.
# 0.4.0 runs the same ONNX models through onnxruntime on SteamOS 3.12/3.13.
WAKE_PACKAGES = ("openwakeword==0.4.0", "onnxruntime")
CONFIRM = {"go ahead", "yes", "do it", "yes go ahead", "yeah", "yep"}
CANCEL = {"cancel", "stop"}
IDLE_NOTE = "The speech model closes after each line."

Fetcher = Callable[[str, str, Callable[[str, float], None] | None], None]
Which = Callable[[str], str | None]
PopenFactory = Callable[..., Any]
Runner = Callable[[list[str], dict[str, str]], None]

WAKE_WORKER = """\
import inspect
import json
import os
import shutil
import sys

os.nice(15)
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

def main() -> None:
    import numpy as np
    import openwakeword
    from openwakeword.model import Model

    source = os.environ.get("DECKLING_MODEL_DIR") or ""
    dest = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(source):
        for name in os.listdir(source):
            if name.endswith(".onnx"):
                target = os.path.join(dest, name)
                if not os.path.exists(target):
                    shutil.copy2(os.path.join(source, name), target)
    path = os.environ["DECKLING_WAKE_MODEL"]
    params = inspect.signature(Model.__init__).parameters
    if "wakeword_models" in params:
        model = Model(wakeword_models=[path], inference_framework="onnx")
    else:
        model = Model(wakeword_model_paths=[path])
    threshold = float(os.environ.get("DECKLING_WAKE_THRESHOLD") or "0.5")
    frame = 1280 * 2
    while True:
        chunk = sys.stdin.buffer.read(frame)
        if len(chunk) < frame:
            break
        audio = np.frombuffer(chunk, dtype=np.int16)
        scores = model.predict(audio)
        best = max((float(value) for value in scores.values()), default=0.0)
        heard = best >= threshold
        if heard or os.environ.get("DECKLING_WAKE_REPORT") == "1":
            sys.stdout.write(json.dumps({"wake": heard, "score": round(best, 4)}) + "\\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
"""

FASTER_WORKER = """\
import sys

def main() -> None:
    from faster_whisper import WhisperModel

    download_root = sys.argv[3] if len(sys.argv) > 3 else None
    model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8", download_root=download_root)
    segments, _info = model.transcribe(sys.argv[2], language="en", vad_filter=True)
    print(" ".join(segment.text.strip() for segment in segments if segment.text).strip())

if __name__ == "__main__":
    main()
"""

POST_WAKE_WORKER = """\
import json
import os
import sys

def main() -> int:
    sys.path.insert(0, os.environ.get("DECKLING_PY_MODULES") or "")
    from ai_assistant.hearing import capture_utterance

    result = capture_utterance(
        os.environ.get("DECKLING_SETTINGS") or "",
        os.environ.get("DECKLING_RUNTIME") or "",
        os.environ.get("DECKLING_PYTHON") or "",
    )
    sys.stdout.write(json.dumps(result) + "\\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1

if __name__ == "__main__":
    raise SystemExit(main())
"""


def _failure_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    if len(text) > 240:
        text = text[:240].rstrip() + "..."
    return f"{type(exc).__name__}: {text}"[:300]


def capture_utterance(settings_dir: str, runtime_dir: str, python: str) -> dict[str, Any]:
    """Record and transcribe one line. Runs in a child process so a crash stays there."""
    try:
        store = Store(settings_dir, runtime_dir)
        engine = HearingEngine(store, python=python or None)
        engine.autostart = False
        engine._chime()
        engine._set_phase("recording", "Hearing you")
        pcm = engine._record()
        engine._done_chime()
        if not pcm:
            return {"ok": True, "text": "", "empty": True}
        engine._set_phase("transcribing", "Transcribing")
        path = engine._keep_or_temp(pcm)
        try:
            text = engine.transcribe_file(path).strip()
        finally:
            if not engine.public()["debug_audio"]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return {"ok": True, "text": text, "empty": not bool(text)}
    except BaseException as exc:
        return {"ok": False, "error": _failure_text(exc), "trace": traceback.format_exc()[-1200:]}


def public_hearing(config: dict[str, Any] | None, phase: str = "off") -> dict[str, Any]:
    hearing = normalize_hearing((config or {}).get("hearing") if isinstance(config, dict) else None)
    hearing["phase"] = phase
    hearing["wake_models"] = [{"id": item["id"], "label": item["label"]} for item in WAKE_MODELS]
    hearing["stt_models"] = list(STT_MODELS)
    hearing["idle_note"] = IDLE_NOTE
    return hearing


def threshold_for(sensitivity: float) -> float:
    clamped = max(0.0, min(1.0, float(sensitivity)))
    return round(0.85 - 0.6 * clamped, 3)


def normalize_phrase(text: str) -> str:
    cleaned = "".join(char.lower() if char.isascii() and (char.isalnum() or char == " ") else " " for char in text)
    return " ".join(cleaned.split())


def wants_quiet(text: str) -> bool:
    cleaned = normalize_phrase(text)
    if cleaned in {"stop", "stop talking", "shut up", "be quiet"}:
        return True
    return cleaned.startswith(("stop talking", "shut up", "be quiet"))


def classify_phrase(text: str, pending: bool, speaking: bool = False) -> str:
    cleaned = normalize_phrase(text)
    if not cleaned:
        return "ignore"
    if cleaned == "stop listening" or cleaned.startswith("stop listening"):
        return "stop_listening"
    if speaking and wants_quiet(cleaned):
        return "stop_talking"
    if speaking and wants_screen_look(text):
        return "screen"
    if speaking:
        return "ignore"
    if cleaned in {"new chat", "start a new chat"}:
        return "new_chat"
    if wants_screen_look(text):
        return "screen"
    if pending and cleaned in CONFIRM:
        return "confirm"
    if cleaned in CANCEL and pending:
        return "cancel"
    return "message"


def whisper_archive_url(machine: str) -> str:
    url = WHISPER_ARCHIVES.get(machine)
    if not url:
        raise RuntimeError(f"whisper.cpp has no Linux build for {machine}.")
    return url


def pip_command(python: str, target: str, packages: tuple[str, ...]) -> list[str]:
    return [python, "-m", "pip", "install", "--target", target, "--upgrade", "--no-warn-script-location", *packages]


def beep_pcm(rate: int = RATE, ms: int = 140, freq: int = 880) -> bytes:
    count = rate * ms // 1000
    samples = array.array("h")
    for index in range(count):
        fade = math.sin(math.pi * index / max(1, count))
        samples.append(int(fade * 0.25 * 32767 * math.sin(2 * math.pi * freq * index / rate)))
    return samples.tobytes()


def tick_pcm(rate: int = RATE, ms: int = 40, freq: int = 520) -> bytes:
    """A quiet click used while a reply is still being written. Off unless enabled."""
    count = rate * ms // 1000
    samples = array.array("h")
    for index in range(count):
        fade = math.sin(math.pi * index / max(1, count))
        samples.append(int(fade * 0.08 * 32767 * math.sin(2 * math.pi * freq * index / rate)))
    return samples.tobytes()


def play_pcm(pcm: bytes) -> None:
    from .voice import playback_command

    try:
        argv, env = playback_command(RATE, shutil.which)
    except RuntimeError:
        return
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        if proc.stdin is not None:
            proc.stdin.write(pcm)
            proc.stdin.close()
        proc.wait(timeout=2)
    except Exception:
        _kill(proc)


def done_pcm(rate: int = RATE, ms: int = 220) -> bytes:
    """A short descending tone, distinct from the wake-word ding."""
    count = rate * ms // 1000
    samples = array.array("h")
    phase = 0.0
    for index in range(count):
        fade = math.sin(math.pi * index / max(1, count))
        freq = 740 - (420 * index / max(1, count))
        phase += 2 * math.pi * freq / rate
        samples.append(int(fade * 0.25 * 32767 * math.sin(phase)))
    return samples.tobytes()


def write_wav(path: str, pcm: bytes, rate: int = RATE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    os.chmod(path, 0o600)


def _download(url: str, dest: str, progress: Callable[[str, float], None] | None = None) -> None:
    import urllib.request

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    temporary = dest + ".partial"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response, open(temporary, "wb") as handle:
        total = int(response.headers.get("Content-Length") or "0")
        read = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            handle.write(chunk)
            read += len(chunk)
            if progress and total:
                progress("Downloading", min(0.99, read / total))
    os.replace(temporary, dest)


class HearingEngine:
    def __init__(
        self,
        store: Store,
        *,
        notify: Callable[[dict[str, Any]], None] | None = None,
        on_command: Callable[[str, str], None] | None = None,
        pending: Callable[[], bool] | None = None,
        speaking: Callable[[], bool] | None = None,
        fetch: Fetcher | None = None,
        popen: PopenFactory | None = None,
        which: Which | None = None,
        run: Runner | None = None,
        machine: str | None = None,
        python: str | None = None,
    ) -> None:
        self.store = store
        self.notify = notify or (lambda _payload: None)
        self.on_command = on_command or (lambda _action, _text: None)
        self.pending = pending or (lambda: False)
        self.speaking = speaking or (lambda: False)
        self.fetch = fetch or _download
        self.popen = popen or subprocess.Popen
        self.which = which or shutil.which
        self.run = run or _run
        self.machine = machine or _machine()
        self._explicit_python = python
        self.python = python or _resolve_python()
        self.autostart = True
        self._phase = "off"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._procs: list[Any] = []
        self._game = False
        self._sleeping = False
        self._busy = False
        self._last_failure = ""
        self._test_stop = threading.Event()
        self._test_thread: threading.Thread | None = None

    def public(self) -> dict[str, Any]:
        try:
            config = self.store.load_config()
        except (OSError, ValueError):
            config = {}
        return public_hearing(config, self._phase)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(patch)
        if "wake_model" in cleaned and cleaned["wake_model"] not in {item["id"] for item in WAKE_MODELS}:
            cleaned["wake_model"] = "hey_jarvis"
        if "stt_model" in cleaned and cleaned["stt_model"] not in STT_MODELS:
            cleaned["stt_model"] = "tiny.en"
        self.store.update_hearing(cleaned)
        if self.autostart and self.public()["wake_enabled"]:
            self.start()
        elif not self.public()["wake_enabled"]:
            self.stop()
        return self.public()

    def set_activity(self, game_running: bool, sleeping: bool) -> dict[str, Any]:
        self._game = bool(game_running)
        self._sleeping = bool(sleeping)
        if self._paused() and self._phase == "listening":
            self._set_phase("paused", "Listening is paused")
            self._stop_procs()
        elif self.public()["wake_enabled"] and not self._paused() and self._phase in {"paused", "off"}:
            self.start()
        return self.public()

    def start(self) -> None:
        if not self.autostart or self._paused():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="deckling-wake", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._stop_procs()
        self._set_phase("off", "")

    def begin_ptt(self) -> None:
        if self._busy:
            return
        thread = threading.Thread(target=self._ptt, name="deckling-ptt", daemon=True)
        thread.start()

    def install(self) -> dict[str, Any]:
        hearing = self.public()
        self._prepare_python()
        self._say("Downloading the wake word model", 0.05)
        try:
            self._download_wake_files(hearing["wake_model"])
        except Exception as exc:  # noqa: BLE001 - shown in settings, the plugin keeps running
            self.store.update_hearing(
                {"wake_error": f"Wake word could not be downloaded. Push to talk still works. {exc}"[:500]}
            )
        else:
            self._say("Installing openWakeWord", 0.35)
            try:
                self._pip(WAKE_PACKAGES)
                self.store.update_hearing({"wake_error": ""})
            except Exception as exc:  # noqa: BLE001
                self.store.update_hearing(
                    {
                        "wake_error": (
                            "Wake word could not be installed on this SteamOS. "
                            f"Push to talk still works. {exc}"
                        )[:500]
                    }
                )
        try:
            backend = self._install_stt(hearing["stt_model"])
        except Exception as exc:  # noqa: BLE001 - the listen loop keeps running
            self.store.update_hearing(
                {
                    "stt_backend": "",
                    "install_message": f"Speech recognition failed. {exc}"[:500],
                    "install_progress": 0,
                }
            )
            self.notify({"type": "hearing", "phase": "error", "message": f"Speech recognition failed. {exc}"[:300]})
            return self.public()
        self.store.update_hearing(
            {
                "stt_backend": backend,
                "install_message": f"Speech recognition: {backend}. {IDLE_NOTE}",
                "install_progress": 1,
            }
        )
        return self.public()

    def _prepare_python(self) -> None:
        """Tests pass an interpreter. The Deck downloads one that has pip."""
        if self._explicit_python:
            self.python = self._explicit_python
            return
        base = ensure_runtime_python(self.store.runtime_dir, self.fetch, self._progress, self.machine)
        self.python = ensure_voice_venv(self.store.runtime_dir, base)

    def dispatch(self, text: str) -> str:
        action = classify_phrase(text, self.pending(), self.speaking())
        if action == "ignore":
            self._set_phase("listening" if self.public()["wake_enabled"] else "off", "")
            return action
        if action == "stop_talking":
            self.on_command("stop_talking", text)
            self._set_phase("listening" if self.public()["wake_enabled"] else "off", "")
            return action
        payload = {"type": "hearing", "phase": "transcript", "transcript": text, "action": action}
        self.notify(payload)
        if action == "stop_listening":
            self.update({"wake_enabled": False})
            self._set_phase("off", "Stopped listening")
            return action
        if action == "confirm":
            self.on_command("confirm", "Yes, go ahead.")
            return action
        self.on_command(action, text)
        return action

    def transcribe_file(self, path: str) -> str:
        hearing = self.public()
        backend = hearing["stt_backend"] or "whisper.cpp"
        if backend == "faster-whisper":
            return self._transcribe_faster(path, hearing["stt_model"])
        return self._transcribe_whisper_cpp(path, hearing["stt_model"])

    def _loop(self) -> None:
        """Supervise wake and post-wake. A worker failure restarts the loop."""
        try:
            self.install()
        except BaseException as exc:
            self.report_failure(exc)
            self._stop.wait(2)
            if self._stop.is_set():
                return
        if self.public()["wake_error"]:
            self.report_failure(RuntimeError(self.public()["wake_error"]))
            self._stop.wait(2)
        while not self._stop.is_set():
            if self._test_thread is not None and self._test_thread.is_alive():
                self._stop.wait(0.3)
                continue
            if self._paused():
                self._set_phase("paused", "Listening is paused")
                self._stop.wait(1)
                continue
            started = time.monotonic()
            try:
                if self._listen_once():
                    self._utterance_isolated()
            except BaseException as exc:
                self.report_failure(exc)
                self._stop.wait(2)
                continue
            if time.monotonic() - started < 1 and not self._stop.is_set():
                self._stop.wait(2)

    def report_failure(self, exc: BaseException) -> None:
        self._surface(_failure_text(exc))

    def _surface(self, message: str) -> None:
        text = message[:300]
        if text == self._last_failure:
            self._phase = "error"
            return
        self._last_failure = text
        remember(f"Voice pipeline: {text}")
        self._set_phase("error", text)
        self.notify({"type": "hearing", "phase": "error", "message": text, "toast": True})

    def _argv(self, *args: str) -> list[str]:
        if frozen_runtime() and os.path.realpath(self.python) == os.path.realpath(sys.executable):
            raise RuntimeError(
                "Voice models need the system Python. "
                f"Decky's PluginLoader cannot run them ({sys.executable})."
            )
        return [self.python, *args]

    def _ptt(self) -> None:
        self._busy = True
        self._stop_procs()
        try:
            if not self.public()["stt_backend"]:
                self.install()
            self._chime()
            self._set_phase("recording", "Hearing you")
            pcm = self._record()
            self._done_chime()
            self._finish_pcm(pcm)
        except Exception as exc:  # noqa: BLE001
            self._set_phase("error", str(exc)[:300])
        finally:
            self._busy = False
            if self.public()["wake_enabled"] and not self._stop.is_set():
                self.start()

    def _utterance(self) -> None:
        self._busy = True
        try:
            self._chime()
            self.notify({"type": "hearing", "phase": "heard", "message": "Listening"})
            self._set_phase("recording", "Hearing you")
            pcm = self._record()
            self._done_chime()
            self._finish_pcm(pcm)
        finally:
            self._busy = False

    def _utterance_isolated(self) -> None:
        """Post-wake work runs in another process. The plugin process only reads the result."""
        self._busy = True
        self.notify({"type": "hearing", "phase": "heard", "message": "Listening"})
        try:
            result = self._run_voice_worker()
        except BaseException as exc:
            self.report_failure(exc)
            return
        finally:
            self._busy = False
        if not result.get("ok"):
            self._surface(str(result.get("error") or "voice worker failed"))
            return
        self._last_failure = ""
        text = str(result.get("text") or "").strip()
        if text:
            self.dispatch(text)
        phase = "listening" if self.public()["wake_enabled"] and not self._paused() else "off"
        self._set_phase(phase, "" if text else "Didn't catch that")

    def _run_voice_worker(self) -> dict[str, Any]:
        worker = self._write_worker("post_wake_worker.py", POST_WAKE_WORKER)
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        env["DECKLING_VOICE_WORKER"] = "1"
        env["DECKLING_PY_MODULES"] = str(os.path.dirname(os.path.dirname(__file__)))
        env["DECKLING_SETTINGS"] = self.store.settings_dir
        env["DECKLING_RUNTIME"] = self.store.runtime_dir
        env["DECKLING_PYTHON"] = self.python
        env["PYTHONNOUSERSITE"] = "1"
        proc = self.popen(
            self._argv(worker),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        with self._lock:
            self._procs.append(proc)
        try:
            out, err = proc.communicate(timeout=120)
        except Exception:
            _kill(proc)
            raise
        payload = _last_json(out or b"")
        if payload:
            return payload
        detail = (err or b"").decode("utf-8", "replace").strip()[-300:]
        code = getattr(proc, "returncode", 1)
        return {"ok": False, "error": detail or f"voice worker exited {code}"}

    def _finish_pcm(self, pcm: bytes) -> None:
        if not pcm:
            self._set_phase("listening" if self.public()["wake_enabled"] else "off", "Didn't catch that")
            return
        self._set_phase("transcribing", "Transcribing")
        path = self._keep_or_temp(pcm)
        try:
            text = self.transcribe_file(path).strip()
        finally:
            if not self.public()["debug_audio"]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if text:
            self.dispatch(text)
        phase = "listening" if self.public()["wake_enabled"] and not self._paused() else "off"
        self._set_phase(phase, "")

    def _listen_once(self) -> bool:
        self._set_phase("listening", "Listening for the wake word")
        worker = self._write_worker("wake_worker.py", WAKE_WORKER)
        model = self._model_path(self.public()["wake_model"])
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        env["PYTHONPATH"] = self._target()
        env["DECKLING_WAKE_MODEL"] = model
        env["DECKLING_MODEL_DIR"] = os.path.dirname(model)
        env["DECKLING_WAKE_THRESHOLD"] = str(threshold_for(self.public()["sensitivity"]))
        capture, capture_env = capture_command(self.which, source=self._input_source())
        mic = self.popen(capture, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=capture_env)
        brain = self.popen(
            self._argv(worker),
            stdin=mic.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        with self._lock:
            self._procs.extend([mic, brain])
        try:
            assert brain.stdout is not None
            line = brain.stdout.readline()
            if not line or self._stop.is_set():
                return False
            try:
                payload = json.loads(line.decode("utf-8", "replace"))
            except (UnicodeError, json.JSONDecodeError, ValueError):
                return False
            woke = bool(isinstance(payload, dict) and payload.get("wake"))
            if not woke:
                self._note_mic_error(mic)
            return woke
        finally:
            self._stop_procs()

    def _input_source(self) -> str:
        saved = str(self.public().get("mic_source") or "")
        try:
            sources = list_input_sources(self.which)
        except Exception:
            sources = []
        return preferred_source(sources, saved)

    def list_mics(self) -> dict[str, Any]:
        try:
            sources = list_input_sources(self.which)
        except Exception as exc:
            return {"ok": False, "error": _failure_text(exc), "mics": []}
        return {
            "ok": True,
            "mics": sources,
            "selected": preferred_source(sources, str(self.public().get("mic_source") or "")),
        }

    def mic_level(self) -> dict[str, Any]:
        source = self._input_source()
        try:
            argv, env = capture_command(self.which, source=source)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "level": 0}
        proc = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            assert proc.stdout is not None
            chunk = proc.stdout.read(RATE * 2 // 5)
        finally:
            _kill(proc)
        if not chunk:
            err = b""
            if proc.stderr is not None:
                try:
                    err = proc.stderr.read(300)
                except Exception:
                    err = b""
            detail = err.decode("utf-8", "replace").strip()
            if detail:
                return {"ok": False, "error": detail[:300], "level": 0, "source": source}
        peak = rms(chunk) / 32767 if chunk else 0.0
        return {"ok": True, "level": round(min(1.0, peak), 3), "source": source}

    def start_wake_test(self) -> dict[str, Any]:
        self._test_stop.set()
        self._stop_procs()
        previous = self._test_thread
        if previous is not None and previous.is_alive():
            previous.join(timeout=2)
        self._test_stop = threading.Event()
        self._test_thread = threading.Thread(target=self._wake_test, name="deckling-wake-test", daemon=True)
        self._test_thread.start()
        return {"ok": True, "hearing": self.public()}

    def stop_wake_test(self) -> dict[str, Any]:
        self._test_stop.set()
        self._stop_procs()
        return {"ok": True, "hearing": self.public()}

    def _wake_test(self) -> None:
        try:
            if not os.path.isfile(self._model_path(self.public()["wake_model"])):
                self.install()
        except Exception as exc:
            self.report_failure(exc)
            return
        self._set_phase("listening", "Say the wake word")
        worker = self._write_worker("wake_worker.py", WAKE_WORKER)
        model = self._model_path(self.public()["wake_model"])
        env = os.environ.copy()
        env.pop("LD_LIBRARY_PATH", None)
        env["PYTHONPATH"] = self._target()
        env["DECKLING_WAKE_MODEL"] = model
        env["DECKLING_MODEL_DIR"] = os.path.dirname(model)
        env["DECKLING_WAKE_THRESHOLD"] = str(threshold_for(self.public()["sensitivity"]))
        env["DECKLING_WAKE_REPORT"] = "1"
        try:
            capture, capture_env = capture_command(self.which, source=self._input_source())
        except RuntimeError as exc:
            self.report_failure(exc)
            return
        mic = self.popen(capture, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=capture_env)
        brain = self.popen(
            self._argv(worker),
            stdin=mic.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        with self._lock:
            self._procs.extend([mic, brain])
        deadline = time.monotonic() + 15
        try:
            assert brain.stdout is not None
            while not self._test_stop.is_set() and time.monotonic() < deadline:
                line = brain.stdout.readline()
                if not line:
                    self._note_mic_error(mic)
                    err = b""
                    if brain.stderr is not None:
                        try:
                            err = brain.stderr.read(300)
                        except Exception:
                            err = b""
                    detail = err.decode("utf-8", "replace").strip()
                    if detail:
                        self._surface(detail)
                    break
                try:
                    payload = json.loads(line.decode("utf-8", "replace"))
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    continue
                score = payload.get("score")
                heard = bool(payload.get("wake"))
                self.notify(
                    {
                        "type": "hearing",
                        "phase": "wake_score",
                        "message": f"Score {score}" + (" · heard the wake word" if heard else ""),
                        "score": score,
                    }
                )
        except Exception as exc:
            self.report_failure(exc)
        finally:
            self._stop_procs()
            phase = "listening" if self.public()["wake_enabled"] and not self._paused() else "off"
            self._set_phase(phase, "")

    def _note_mic_error(self, proc: Any) -> None:
        code = getattr(proc, "returncode", None)
        if code in {None, 0}:
            return
        err = b""
        if getattr(proc, "stderr", None) is not None:
            try:
                err = proc.stderr.read(300)
            except Exception:
                err = b""
        detail = err.decode("utf-8", "replace").strip()
        if detail:
            self._surface(detail)

    def _record(self) -> bytes:
        argv, env = capture_command(self.which, source=self._input_source())
        proc = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        with self._lock:
            self._procs.append(proc)
        try:
            assert proc.stdout is not None
            return _read_until_silence(proc.stdout, self._stop)
        finally:
            _kill(proc)

    def _keep_or_temp(self, pcm: bytes) -> str:
        if self.public()["debug_audio"]:
            folder = os.path.join(self.store.runtime_dir, "debug-audio")
        else:
            folder = os.path.join(self.store.runtime_dir, "hearing", "tmp")
        os.makedirs(folder, exist_ok=True)
        os.chmod(folder, 0o700)
        path = os.path.join(folder, f"utterance-{uuid.uuid4().hex}.wav")
        write_wav(path, pcm)
        return path

    def _download_wake_files(self, model_id: str) -> None:
        folder = os.path.join(self.store.runtime_dir, "hearing", "models")
        os.makedirs(folder, exist_ok=True)
        names = list(FEATURE_MODELS)
        match = next(item for item in WAKE_MODELS if item["id"] == model_id)
        names.append(match["file"])
        for name in names:
            dest = os.path.join(folder, name)
            if not os.path.isfile(dest):
                self.fetch(f"{WAKE_RELEASE}/{name}", dest, self._progress)

    def _install_stt(self, model: str) -> str:
        self._say("Trying faster-whisper", 0.55)
        try:
            self._pip(FASTER_PACKAGES)
            self._write_worker("faster_worker.py", FASTER_WORKER)
            return "faster-whisper"
        except Exception:
            self._say("Installing whisper.cpp", 0.7)
            self._install_whisper_cpp(model)
            return "whisper.cpp"

    def _install_whisper_cpp(self, model: str) -> None:
        folder = os.path.join(self.store.runtime_dir, "hearing", "whisper")
        os.makedirs(folder, exist_ok=True)
        archive = os.path.join(folder, "whisper.tar.gz")
        if not self._find_whisper_bin():
            self.fetch(whisper_archive_url(self.machine), archive, self._progress)
            safe_extract(archive, folder)
            binary = self._find_whisper_bin()
            if binary:
                os.chmod(binary, 0o755)
        dest = os.path.join(folder, f"ggml-{model}.bin")
        if not os.path.isfile(dest):
            self._say(f"Downloading {model}", 0.85)
            self.fetch(GGML_URL.format(model=model), dest, self._progress)

    def _pip(self, packages: tuple[str, ...]) -> None:
        env = deck_audio_env(os.geteuid())
        env.pop("LD_LIBRARY_PATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        python = self._argv()[0]
        if self._explicit_python:
            target = self._target()
            os.makedirs(target, exist_ok=True)
            command = pip_command(python, target, packages)
        else:
            command = [python, "-m", "pip", "install", "--disable-pip-version-check", *packages]
        self.run(command, env)

    def _transcribe_whisper_cpp(self, path: str, model: str) -> str:
        binary = self._find_whisper_bin()
        if not binary:
            raise RuntimeError("whisper.cpp is not installed yet.")
        model_path = os.path.join(self.store.runtime_dir, "hearing", "whisper", f"ggml-{model}.bin")
        if not os.path.isfile(model_path):
            self.fetch(GGML_URL.format(model=model), model_path, self._progress)
        env = deck_audio_env(os.geteuid())
        completed = subprocess.run(
            [binary, "-m", model_path, "-f", path, "-l", "en", "-nt", "-np"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "whisper.cpp failed").strip()[-300:])
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _transcribe_faster(self, path: str, model: str) -> str:
        worker = self._write_worker("faster_worker.py", FASTER_WORKER)
        env = deck_audio_env(os.geteuid())
        env.pop("LD_LIBRARY_PATH", None)
        root = os.path.join(self.store.runtime_dir, "hearing", "faster")
        os.makedirs(root, exist_ok=True)
        env["PYTHONPATH"] = self._target()
        env["HF_HOME"] = os.path.join(self.store.runtime_dir, "hearing", "hf")
        env["HUGGINGFACE_HUB_CACHE"] = os.path.join(env["HF_HOME"], "hub")
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        completed = subprocess.run(
            self._argv(worker, model, path, root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "faster-whisper failed").strip()[-300:])
        return completed.stdout.strip()

    def _chime(self) -> None:
        self._play_pcm(beep_pcm())
        self.notify({"type": "hearing", "phase": "toast", "message": "Listening"})

    def _done_chime(self) -> None:
        if not self.public().get("done_sound", True):
            return
        self._play_pcm(done_pcm())
        self.notify({"type": "hearing", "phase": "toast", "message": "Thinking"})

    def _play_pcm(self, pcm: bytes) -> None:
        try:
            argv, env = playback_command(RATE, self.which)
        except RuntimeError:
            return
        proc = self.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            if proc.stdin is not None:
                proc.stdin.write(pcm)
                proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            _kill(proc)

    def _model_path(self, model_id: str) -> str:
        match = next(item for item in WAKE_MODELS if item["id"] == model_id)
        return os.path.join(self.store.runtime_dir, "hearing", "models", match["file"])

    def _find_whisper_bin(self) -> str:
        root = os.path.join(self.store.runtime_dir, "hearing", "whisper")
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name in {"whisper-cli", "main"}:
                    return os.path.join(dirpath, name)
        return ""

    def _target(self) -> str:
        return os.path.join(self.store.runtime_dir, "hearing", "py")

    def _write_worker(self, name: str, source: str) -> str:
        folder = os.path.join(self.store.runtime_dir, "hearing")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return path

    def _paused(self) -> bool:
        if self._sleeping:
            return True
        return bool(self.public()["battery_saver"] and self._game)

    def _set_phase(self, phase: str, message: str) -> None:
        self._phase = phase
        self.notify({"type": "hearing", "phase": phase, "message": message})

    def _say(self, message: str, fraction: float) -> None:
        self._progress(message, fraction)

    def _progress(self, message: str, fraction: float) -> None:
        percent = max(0, min(100, int(float(fraction) * 100)))
        text = f"{message} ({percent}%)"
        self.store.update_hearing({"install_message": text[:500], "install_progress": float(fraction)})
        self.notify({"type": "hearing", "phase": "install", "message": text, "progress": fraction})

    def _stop_procs(self) -> None:
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        for proc in procs:
            _kill(proc)


def _read_until_silence(stream: Any, stop: threading.Event) -> bytes:
    width = RATE * 30 // 1000 * 2
    pcm = bytearray()
    max_bytes = RATE * 2 * MAX_MS // 1000
    no_speech_bytes = RATE * 2 * NO_SPEECH_MS // 1000
    while len(pcm) < max_bytes and not stop.is_set():
        chunk = stream.read(width)
        if not chunk:
            break
        pcm.extend(chunk)
        region = speech_region(bytes(pcm))
        if region is not None:
            return bytes(pcm[region[0] : region[1]])
        if len(pcm) >= no_speech_bytes and speech_region(bytes(pcm), min_speech_ms=1) is None:
            # Still no speech. Give up instead of recording the room forever.
            if _quiet(bytes(pcm)):
                return b""
    region = speech_region(bytes(pcm))
    if region is not None:
        return bytes(pcm[region[0] : region[1]])
    return bytes(pcm) if not _quiet(bytes(pcm)) else b""


def _quiet(pcm: bytes) -> bool:
    width = RATE * 30 // 1000 * 2
    if len(pcm) < width:
        return True
    return all(rms(pcm[index : index + width]) < THRESHOLD for index in range(0, len(pcm) - width + 1, width))


def _kill(proc: Any) -> None:
    try:
        proc.kill()
    except Exception:
        return
    try:
        proc.wait(timeout=1)
    except Exception:
        return


def _run(command: list[str], env: dict[str, str]) -> None:
    completed = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise RuntimeError(detail[-400:])


def _machine() -> str:
    import platform

    return platform.machine()


def _resolve_python() -> str:
    """Placeholder until install() downloads the private interpreter."""
    return sys.executable


def _last_json(raw: bytes) -> dict[str, Any]:
    for line in reversed(raw.decode("utf-8", "replace").splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}
