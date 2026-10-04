import { addEventListener, removeEventListener } from "@decky/api";
import { PanelSection, PanelSectionRow } from "@decky/ui";
import { useEffect, useState } from "react";
import { getState, listMics, micLevel, saveHearing, stopWakeTest, testWake } from "../api";
import { DeckRow } from "../DeckRow";
import { errorMessage } from "../retry";
import type { BackendEvent, HearingSettings } from "../types";

export function HearingSection({
  hearing,
  onHearing,
  onError,
}: {
  hearing: HearingSettings;
  onHearing: (hearing: HearingSettings) => void;
  onError: (message: string) => void;
}) {
  const save = async (patch: Partial<HearingSettings>) => {
    try {
      const result = await saveHearing(patch);
      if (!result.ok || !result.hearing) {
        onError(result.error || "Could not save listening settings");
        return;
      }
      onHearing(result.hearing);
    } catch (err) {
      onError(errorMessage(err, "Could not save listening settings"));
    }
  };

  const percent = Math.round(hearing.sensitivity * 100);
  const [about, setAbout] = useState(false);
  const [mics, setMics] = useState<{ name: string; label: string; default?: boolean }[]>([]);
  const [level, setLevel] = useState(0);
  const [score, setScore] = useState("");
  const [testing, setTesting] = useState(false);
  const progress = Math.round((hearing.install_progress || 0) * 100);
  const installing = Boolean(hearing.install_message) && progress > 0 && progress < 100;

  useEffect(() => {
    let cancelled = false;
    void listMics().then((result) => {
      if (!cancelled && result.ok) {
        setMics(result.mics || []);
      }
    });
    if (testing) {
      return () => {
        cancelled = true;
      };
    }
    const timer = window.setInterval(() => {
      void micLevel().then((result) => {
        if (result.ok && typeof result.level === "number") {
          setLevel(result.level);
        }
      });
    }, 700);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [hearing.mic_source, testing]);

  useEffect(() => {
    const listener = addEventListener<[BackendEvent]>("deckling_event", (event) => {
      if (event.type === "hearing" && event.phase === "wake_score") {
        setScore(event.message || (typeof event.score === "number" ? `Score ${event.score}` : ""));
      }
      if (event.type === "hearing" && event.phase === "error" && event.message) {
        onError(event.message);
      }
    });
    return () => removeEventListener("deckling_event", listener);
  }, [onError]);

  useEffect(() => {
    if (!hearing.wake_enabled || !installing) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      void getState().then((loaded) => {
        if (loaded.hearing) {
          onHearing({ ...hearing, ...loaded.hearing });
        }
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [hearing.install_message, hearing.wake_enabled, installing, onHearing]);

  return (
    <PanelSection title="Voice">
      <PanelSectionRow>
        <div>Models download onto this Deck the first time listening is on.</div>
      </PanelSectionRow>
      <DeckRow layout="below" onClick={() => void save({ wake_enabled: !hearing.wake_enabled })}>
        {hearing.wake_enabled ? "Wake word: on" : "Wake word: off"}
      </DeckRow>
      <DeckRow layout="below" onClick={() => void save({ mic_source: "" })}>
        {hearing.mic_source ? `Microphone: ${hearing.mic_source}` : "Microphone: system default"}
      </DeckRow>
      {mics.map((mic) => (
        <DeckRow key={mic.name} layout="below" onClick={() => void save({ mic_source: mic.name })}>
          {hearing.mic_source === mic.name || (!hearing.mic_source && mic.default)
            ? `Using ${mic.label}`
            : mic.label}
        </DeckRow>
      ))}
      <PanelSectionRow>
        <div>{`Input level ${Math.round(level * 100)}%`}</div>
        <div style={{ height: "8px", background: "#1b2836", borderRadius: "4px", marginTop: "6px" }}>
          <div style={{ width: `${Math.round(level * 100)}%`, height: "8px", background: "#7fd1c3", borderRadius: "4px" }} />
        </div>
      </PanelSectionRow>
      <DeckRow
        layout="below"
        onClick={() => {
          if (testing) {
            setTesting(false);
            void stopWakeTest();
            return;
          }
          setTesting(true);
          setScore("Listening for the wake word…");
          void testWake().then((result) => {
            if (!result.ok) {
              setTesting(false);
              onError(result.error || "Could not test the wake word");
            }
          });
        }}
      >
        {testing ? "Stop wake word test" : "Test wake word"}
      </DeckRow>
      {score ? (
        <PanelSectionRow>
          <div>{score}</div>
        </PanelSectionRow>
      ) : null}
      {hearing.wake_enabled ? (
        <>
          <PanelSectionRow>
            <label>
              {`Sensitivity ${percent}%`}
              <input
                aria-label="Wake word sensitivity"
                type="range"
                min={0}
                max={100}
                value={percent}
                onChange={(event) => void save({ sensitivity: Number(event.target.value) / 100 })}
                style={{ width: "100%" }}
              />
            </label>
          </PanelSectionRow>
          {hearing.wake_models.map((item) => (
            <DeckRow key={item.id} layout="below" onClick={() => void save({ wake_model: item.id })}>
              {hearing.wake_model === item.id ? `Wake word: ${item.label}` : item.label}
            </DeckRow>
          ))}
          {hearing.stt_models.map((model) => (
            <DeckRow key={model} layout="below" onClick={() => void save({ stt_model: model })}>
              {hearing.stt_model === model ? `Speech model: ${model}` : model}
            </DeckRow>
          ))}
          <DeckRow layout="below" onClick={() => void save({ battery_saver: !hearing.battery_saver })}>
            {hearing.battery_saver ? "Pause while a game is running" : "Keep listening during games"}
          </DeckRow>
          <DeckRow layout="below" onClick={() => void save({ debug_audio: !hearing.debug_audio })}>
            {hearing.debug_audio ? "Debug audio: on" : "Debug audio: off"}
          </DeckRow>
        </>
      ) : null}
      <DeckRow layout="below" onClick={() => void save({ ptt_enabled: !hearing.ptt_enabled })}>
        {hearing.ptt_enabled ? "Push to talk: on" : "Push to talk: off"}
      </DeckRow>
      <DeckRow layout="below" onClick={() => void save({ done_sound: hearing.done_sound === false })}>
        {hearing.done_sound === false ? "Sound when done listening: off" : "Sound when done listening: on"}
      </DeckRow>
      <DeckRow layout="below" onClick={() => void save({ thinking_tick: !hearing.thinking_tick, thinking_tick_set: true })}>
        {hearing.thinking_tick ? "Soft tick while thinking: on" : "Soft tick while thinking: off"}
      </DeckRow>
      {hearing.install_message ? (
        <PanelSectionRow>
          <div>
            {hearing.install_message}
            {installing ? ` ${progress}%` : ""}
          </div>
        </PanelSectionRow>
      ) : null}
      {hearing.wake_error ? (
        <PanelSectionRow>
          <div style={{ color: "#f2b8b5", whiteSpace: "pre-wrap" }}>{hearing.wake_error}</div>
        </PanelSectionRow>
      ) : null}
      <DeckRow layout="below" onClick={() => setAbout((open) => !open)}>
        {about ? "Hide wake word notes" : "Wake word notes"}
      </DeckRow>
      {about ? (
        <PanelSectionRow>
          <div>A custom “hey deckling” model is not bundled. hey jarvis is the built-in word. {hearing.idle_note}</div>
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
}
