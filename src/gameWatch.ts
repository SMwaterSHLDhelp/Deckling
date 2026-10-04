/** Push the focused game to the backend whenever Steam says it changed. */

import { toaster } from "@decky/api";
import { setGameContext } from "./api";
import { emptySnapshot, primeRouter, readLiveGame, watchLiveGame, type GameSnapshot } from "./gameContext";

export const GAME_EVENT = "deckling-game";

let lastSig = "";
let chain: Promise<void> = Promise.resolve();

export async function syncFocusedGame(force = false): Promise<GameSnapshot> {
  let snapshot = emptySnapshot();
  const run = chain.then(async () => {
    snapshot = await readLiveGame();
    const sig = [snapshot.appid, snapshot.name, snapshot.rich_presence, snapshot.achievements_unlocked].join("|");
    if (!force && sig === lastSig) {
      return;
    }
    const result = await setGameContext({ ...snapshot });
    if (!result.ok) {
      return;
    }
    lastSig = sig;
    if (result.notice) {
      toaster.toast({ title: "Deckling", body: result.notice, duration: 3000 });
    }
    window.dispatchEvent(new CustomEvent(GAME_EVENT, { detail: result }));
  });
  chain = run.then(
    () => undefined,
    () => undefined,
  );
  await run;
  return snapshot;
}

export function startGameWatch(): () => void {
  let timer = 0;
  let stopped = false;
  let watch: ReturnType<typeof watchLiveGame> | null = null;
  void (async () => {
    await primeRouter();
    if (stopped) {
      return;
    }
    watch = watchLiveGame(() => {
      void syncFocusedGame(true);
    });
    void syncFocusedGame(true);
    timer = window.setInterval(() => {
      void syncFocusedGame(false);
    }, 4000);
  })();
  return () => {
    stopped = true;
    window.clearInterval(timer);
    watch?.stop();
  };
}
