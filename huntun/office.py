"""Authoritative Office simulation, with durable scenes and read-only snapshots.

The existing JavaScript state machine runs in an embedded V8 context on the
server. Browsers receive its state and interpolate recorded movement history.
"""
from __future__ import annotations

import copy
import json
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from py_mini_racer import MiniRacer

from .config import config_exists, load_team
from .models import BACKEND_LABEL

SOURCE = (Path(__file__).with_name("office.js")).read_text()
LOG = logging.getLogger(__name__)
BOOTSTRAP = """
var __officeClock = 0, __officeRandomState = 1;
Math.random = () => {
  let x = __officeRandomState;
  x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
  __officeRandomState = x >>> 0;
  return __officeRandomState / 4294967296;
};
function hash(name) { let h = 2166136261; for (const c of name) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0; } return h; }
const tr = x => x;
const localStorage = {getItem:() => null, setItem:() => {}};
let scene;
function initializeScene(state, saved, theme, at, seed) {
  __officeClock = at; __officeRandomState = seed;
  scene = makeOffice("", true);
  if (saved) { scene.restoreScene(saved); scene.update(state); }
  else scene.initialize(state, theme);
  return scene.themes;
}
function tickScene(state, at, dt) {
  __officeClock = at;
  if (state) scene.update(state);
  scene.advance(dt);
  return {...scene.checkpoint(), random_state:__officeRandomState};
}
function tickSceneJSON(state, at, dt) {
  return JSON.stringify(tickScene(state ? JSON.parse(state) : null, at, dt));
}
function changeTheme(theme, at) { __officeClock = at; scene.setTheme(theme); return null; }
function snapshotScene() { return {...scene.checkpoint(), random_state:__officeRandomState}; }
"""


class OfficeRuntime:
    """One project scene. Calls are serialized by OfficeService's lock."""

    def __init__(self, store: Any, state: dict[str, Any], *, now: float | None = None, seed: int | None = None) -> None:
        self.store = store
        self.ctx = MiniRacer()
        self.ctx.set_hard_memory_limit(64 * 1024 * 1024)
        self.ctx.set_soft_memory_limit(32 * 1024 * 1024)
        self.ctx.eval("const BLABEL = " + json.dumps(BACKEND_LABEL) + ";\n" + BOOTSTRAP + SOURCE, timeout_sec=5)
        # A reusable function avoids compiling a unique eval script with a new
        # timestamp/input every tick, which otherwise accumulates V8 code memory.
        self.tick_js = self.ctx.eval("tickSceneJSON")
        saved_raw = store.get_control("office_scene", "")
        saved = json.loads(saved_raw) if saved_raw else None
        self.requested_theme = store.get_control("office_theme", "")
        self.last_at = max(time.time() * 1000 if now is None else now,
                           saved.get("at", 0) if saved else 0)
        # Scene timers retain epoch timestamps, while tick intervals follow a
        # monotonic clock. Wall-clock adjustments cannot reorder the history.
        self.clock_at = self.last_at
        self.clock_monotonic = time.monotonic()
        self.last_saved = self.last_at
        self.last_input = state
        self.last_input_at = self.last_at
        self.themes = self.ctx.call("initializeScene", state, saved, self.requested_theme or "regular", self.last_at,
                                   saved["random_state"] if saved else (seed or secrets.randbelow(2**32 - 1) + 1), timeout_sec=5)
        self.scene = self.ctx.call("snapshotScene", timeout_sec=2)
        same_layout = bool(saved and saved.get("layout") == self.scene.get("layout"))
        self.animation_frames = deque(saved.get("animation_frames", []) if same_layout else [], maxlen=64)
        self.animation_speech = saved.get("animation_speech", {}) if saved else {}
        self.animation_characters = saved.get("animation_characters", {}) if same_layout else {}
        self.last_characters = saved.get("chars", {}) if same_layout else self.scene["chars"]
        self.speech_serial = max((int(k) for k in self.animation_speech), default=0)
        self._record_animation()
        self.save()

    def clock_now(self) -> float:
        return self.clock_at + (time.monotonic() - self.clock_monotonic) * 1000

    def tick(self, state: dict[str, Any] | None, now: float | None = None) -> None:
        at = self.clock_now() if now is None else now
        dt = max(0, min(0.1, (at - self.last_at) / 1000))
        self.scene = json.loads(self.tick_js(json.dumps(state) if state is not None else "", at, dt, timeout_sec=2))
        self._record_animation()
        if state is not None:
            self.last_input = state
            self.last_input_at = at
        self.last_at = at
        if at - self.last_saved >= 1000:
            self.save()

    def view(self) -> dict[str, Any]:
        return {**copy.deepcopy(self.scene), "requested_theme": self.requested_theme or self.scene["theme"],
                "theme_initialized": bool(self.requested_theme)}

    def _record_animation(self) -> None:
        """A short authoritative movement history for smooth buffered playback."""
        # Compact pose vectors are decoded by office.js renderChar. Static sprite
        # appearance remains in the canonical scene rather than repeated per tick.
        layout_revision = self.scene.get("layout", {}).get("revision", 0)
        if self.animation_frames and self.animation_frames[-1].get("layout_revision", 0) != layout_revision:
            self.animation_frames.clear()
            self.animation_characters.clear()
            self.last_characters = self.scene["chars"]
        previous = self.animation_frames[-1]["chars"] if self.animation_frames else {}
        speech_ids = {}
        for name, c in self.scene["chars"].items():
            speech_ids[name] = None
            if not c.get("say") or (c.get("sayUntil") or 0) <= self.scene["at"]:
                continue
            speech = {key: c.get(key, 0) for key in ("say", "sayStart", "sayUntil")}
            prior = previous.get(name, [])
            sid = prior[19] if len(prior) > 19 else None
            if self.animation_speech.get(sid) != speech:
                self.speech_serial += 1
                sid = str(self.speech_serial)
                self.animation_speech[sid] = speech
            speech_ids[name] = sid
        frame = {"at": self.scene["at"], "theme": self.scene["theme"], "layout_revision": layout_revision,
                 "chars": {name: [c["px"], c["py"], c["tx"], c["ty"], c["dir"], c["frame"],
                                  c["moving"], c.get("prog", 0), c["mode"], len(c["steps"]),
                                  c.get("carry"), c.get("faceOverride"), c.get("cupAt"), c.get("satAt"),
                                  c.get("hidden", False), c.get("activityPose"), c.get("thinkStyle"),
                                  c.get("saluting", False), c.get("gear"), speech_ids[name]]
                           for name, c in self.scene["chars"].items()}}
        if self.animation_frames and self.animation_frames[-1]["at"] == frame["at"]:
            self.animation_frames[-1] = frame
        else:
            self.animation_frames.append(frame)
        while len(self.animation_frames) > 1 and self.animation_frames[0]["at"] < frame["at"] - 2400:
            self.animation_frames.popleft()
        # A removed actor can still be visible in the buffered frames. Retain
        # its appearance until those frames expire, including across reloads.
        for name, c in self.last_characters.items():
            if name not in self.scene["chars"]:
                self.animation_characters[name] = c
        recorded_names = {name for f in self.animation_frames for name in f["chars"]}
        self.animation_characters = {name: c for name, c in self.animation_characters.items()
                                     if name in recorded_names and name not in self.scene["chars"]}
        self.last_characters = self.scene["chars"]
        used = {pose[19] for f in self.animation_frames for pose in f["chars"].values() if len(pose) > 19 and pose[19]}
        self.animation_speech = {sid: speech for sid, speech in self.animation_speech.items() if sid in used}
        self.scene["animation_frames"] = list(self.animation_frames)
        self.scene["animation_speech"] = dict(self.animation_speech)
        self.scene["animation_characters"] = dict(self.animation_characters)

    def set_theme(self, theme: str, *, initialize: bool = False) -> dict[str, Any]:
        if theme not in self.themes:
            raise ValueError("unknown Office theme")
        if initialize and self.requested_theme:
            return self.view()
        self.ctx.call("changeTheme", theme, self.last_at, timeout_sec=2)
        self.requested_theme = theme
        self.store.set_control("office_theme", theme)
        self.scene = self.ctx.call("snapshotScene", timeout_sec=2)
        self._record_animation()
        self.save()
        return self.view()

    def save(self) -> None:
        self.store.set_control("office_scene", json.dumps(self.scene, separators=(",", ":"), ensure_ascii=False))
        self.last_saved = self.last_at

    def close(self) -> None:
        try:
            self.save()
        finally:
            self.ctx.close()


def office_input(entry: Any) -> dict[str, Any]:
    """Only lifecycle and activity data needed by the simulation."""
    orch = entry.orchestrator
    store = entry.open_store()
    team = orch.team if orch else load_team(entry.path)
    statuses = store.agent_statuses()
    agents = []
    for spec in team:
        info = orch.runtimes[spec.name].info() if orch and spec.name in orch.runtimes else {}
        agents.append({**spec.to_dict(), "live": statuses.get(spec.name),
                       "info": {key: info.get(key) for key in ("backend", "compactions", "compacting", "activity")}})
    return {"agents": agents, "running": store.is_running(), "limits": orch.limits() if orch else {"backends": {}},
            "events": store.office_events()}


class OfficeService:
    """Runs every initialized project's scene, even with no connected browsers."""

    def __init__(self, hub: Any) -> None:
        self.hub = hub
        self.lock = threading.RLock()
        self.runtimes: dict[str, OfficeRuntime] = {}
        self.error: dict[str, str] = {}
        self.retry_at: dict[str, float] = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="huntun-office", daemon=True)
        self.thread.start()

    def _ensure(self, entry: Any) -> OfficeRuntime:
        runtime = self.runtimes.get(entry.id)
        if runtime is None:
            runtime = OfficeRuntime(entry.open_store(), office_input(entry))
            self.runtimes[entry.id] = runtime
        else:
            # An orchestrator may replace the setup/planning Store connection.
            runtime.store = entry.open_store()
        return runtime

    def snapshot(self, entry: Any) -> dict[str, Any]:
        with self.lock:
            if entry.id in self.error:
                raise RuntimeError(self.error[entry.id])
            return self._ensure(entry).view()

    def theme(self, entry: Any, name: str, initialize: bool) -> dict[str, Any]:
        with self.lock:
            return self._ensure(entry).set_theme(name, initialize=initialize)

    def _run(self) -> None:
        deadline = time.monotonic()
        while not self.stop_event.wait(max(0, deadline - time.monotonic())):
            deadline = max(deadline + 0.05, time.monotonic())
            with self.hub.lock:
                entries = list(self.hub.workspaces.values())
            with self.lock:
                existing = {e.id for e in entries}
                for wid in list(self.runtimes):
                    if wid not in existing:
                        self.runtimes.pop(wid).ctx.close()
                        self.error.pop(wid, None)
                        self.retry_at.pop(wid, None)
            for entry in entries:
                # Let a browser read between projects, instead of waiting for
                # all initialized offices to finish their ticks and saves.
                with self.lock:
                    if not config_exists(entry.path) or time.monotonic() < self.retry_at.get(entry.id, 0):
                        continue
                    try:
                        runtime = self._ensure(entry)
                        at = runtime.clock_now()
                        runtime.tick(office_input(entry) if at - runtime.last_input_at >= 250 else None, at)
                        self.error.pop(entry.id, None)
                        self.retry_at.pop(entry.id, None)
                    except Exception as exc:
                        message = f"Office simulation: {type(exc).__name__}: {exc}"
                        if self.error.get(entry.id) != message:
                            LOG.exception("Office simulation failed for %s", entry.id)
                        self.error[entry.id] = message
                        self.retry_at[entry.id] = time.monotonic() + 1

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)
        with self.lock:
            for runtime in self.runtimes.values():
                runtime.close()
            self.runtimes.clear()
