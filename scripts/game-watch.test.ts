import assert from "node:assert/strict";
import { createGameWatch, emptyFocus, noteLifetime, noteMain, resolveFocused } from "../src/gameContext.ts";

const sephiria = { appid: 10, display_name: "Sephiria", rt_last_time_played: 100, display_status: 4 };
const keeper = { appid: 20, display_name: "Graveyard Keeper", rt_last_time_played: 200, display_status: 4 };
const overviews: Record<number, typeof sephiria> = { 10: sephiria, 20: keeper };

const notes: Array<(note: { unAppID: number; bRunning: boolean }) => void> = [];
const steam = {
  GameSessions: {
    RegisterForAppLifetimeNotifications(callback: (note: { unAppID: number; bRunning: boolean }) => void) {
      notes.push(callback);
      return {
        unregister() {
          notes.splice(0, notes.length);
        },
      };
    },
  },
  Apps: {
    GetAppOverviewByAppID(id: number) {
      return overviews[id] || null;
    },
  },
};

let main: typeof sephiria | null = sephiria;
const watch = createGameWatch({
  steam,
  sources: () => ({ main, running: [sephiria, keeper] }),
});

assert.equal(notes.length, 1);
notes[0]({ unAppID: 10, bRunning: true });
assert.equal(watch.peek()?.display_name, "Sephiria");
notes[0]({ unAppID: 20, bRunning: true });
assert.equal(watch.peek()?.display_name, "Graveyard Keeper");
assert.equal(main.display_name, "Sephiria");
notes[0]({ unAppID: 10, bRunning: true });
assert.equal(watch.peek()?.display_name, "Sephiria");
notes[0]({ unAppID: 10, bRunning: false });
notes[0]({ unAppID: 20, bRunning: false });
assert.equal(watch.peek(), null);
watch.stop();

const both = resolveFocused(emptyFocus(), { main: null, running: [sephiria, keeper] });
assert.equal(both?.display_name, "Graveyard Keeper");

let state = noteMain(emptyFocus(), sephiria);
state = noteLifetime(state, { unAppID: 20, bRunning: true });
const focused = resolveFocused(state, {
  main: sephiria,
  running: [sephiria, keeper],
  byId: (id) => overviews[id] || null,
});
assert.equal(focused?.display_name, "Graveyard Keeper");

let lifetimeFirst = noteLifetime(emptyFocus(), { unAppID: 20, bRunning: true });
lifetimeFirst = noteMain(lifetimeFirst, sephiria);
const stillKeeper = resolveFocused(lifetimeFirst, {
  main: sephiria,
  running: [sephiria, keeper],
  byId: (id) => overviews[id] || null,
});
assert.equal(stillKeeper?.display_name, "Graveyard Keeper");

console.log("game watch check passed");
