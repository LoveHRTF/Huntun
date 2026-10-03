"""Durable Office simulation and browser-independent state transitions."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from huntun.office import OfficeRuntime
from huntun.store import Store

AT = 2_000_000_000_000


def state(status: str = "working", *, backend: str = "codex") -> dict:
    return {"agents": [{"name": "dev", "role": "backend", "title": "Dev", "status": "active", "backend": backend,
                        "model": "gpt-6.1-sol", "live": {"status": status},
                        "info": {"backend": backend, "compactions": 12, "compacting": False,
                                 "activity": {"kind": "tool", "at": "2033-01-01", "active": True}}}],
            "running": True, "events": [], "limits": {"backends": {}}}


class OfficeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = 0

    def runtime(self, st: dict, *, saved: str = "", seed: int = 1, now: float = AT, theme: str = "chinese_tech") -> OfficeRuntime:
        self.index += 1
        store = Store(Path(self.tmp.name) / f"office-{self.index}.db")
        self.addCleanup(store.close)
        store.set_control("office_theme", theme)
        if saved:
            store.set_control("office_scene", saved)
        runtime = OfficeRuntime(store, st, now=now, seed=seed)
        self.addCleanup(runtime.close)
        return runtime

    def test_refresh_and_multiple_readers_do_not_reroll_or_mutate_state(self) -> None:
        rt = self.runtime(state("waiting for mention"))
        before = rt.view()
        saved = rt.store.get_control("office_scene", "")
        for _ in range(20):
            self.assertEqual(before, rt.view())
        other_browser = rt.view()
        other_browser["chars"]["dev"]["mode"] = "work"
        other_browser["guardQueue"].append({"k": "fake"})
        self.assertEqual(rt.view(), before)
        self.assertEqual(rt.store.get_control("office_scene", ""), saved)

    def test_restart_preserves_motion_scripts_and_future_random_choices(self) -> None:
        st = state()
        live = self.runtime(st)
        st["agents"].append({**copy.deepcopy(st["agents"][0]), "name": "new-hire"})
        for i in range(1, 25):
            live.tick(st, AT + i * 50)
        live.save()
        before = live.view()
        self.assertTrue(before["chars"]["new-hire"]["setup"])
        restarted = self.runtime(st, saved=live.store.get_control("office_scene", ""), now=AT + 24 * 50)
        self.assertEqual(restarted.view(), before)
        for i in range(25, 100):
            live.tick(st, AT + i * 50)
            restarted.tick(st, AT + i * 50)
            self.assertEqual(restarted.view(), live.view())

    def test_wall_clock_adjustments_do_not_jump_or_reorder_the_animation_history(self) -> None:
        rt = self.runtime(state())
        for i, wall in enumerate((AT / 1000 + 3600, AT / 1000 - 3600, AT / 1000), 1):
            with patch("huntun.office.time.time", return_value=wall), \
                 patch("huntun.office.time.monotonic", return_value=rt.clock_monotonic + i * 0.05):
                rt.tick(None)
            self.assertAlmostEqual(rt.view()["at"], AT + i * 50, delta=0.01)
        times = [f["at"] for f in rt.view()["animation_frames"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(49 <= b - a <= 51 for a, b in zip(times, times[1:])))

    def test_restart_after_a_backward_clock_change_preserves_the_scene_timeline(self) -> None:
        st = state()
        rt = self.runtime(st)
        rt.tick(None, AT + 50)
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=AT - 5000)
        self.assertEqual(restored.view(), rt.view())
        self.assertEqual(restored.last_at, AT + 50)

    def test_model_names_do_not_change_idle_choices(self) -> None:
        observed = []
        for backend in ("codex", "claude-code", "kimi", "api", "deepseek", "ollama", "vllm", "llamacpp"):
            rt = self.runtime(state("idle", backend=backend))
            c = rt.view()["chars"]["dev"]
            observed.append((c["mode"], c["smoke"], c["goal"]))
        self.assertTrue(all(item == observed[0] for item in observed))

    def test_simultaneous_hires_finish_their_moves_instead_of_blocking_the_door(self) -> None:
        for backends in (("codex",) * 4, ("codex", "claude-code", "pi-clm", "api")):
            with self.subTest(backends=backends):
                st = state()
                rt = self.runtime(st)
                names = [f"hire-{i}" for i in range(4)]
                st["agents"].extend({**copy.deepcopy(st["agents"][0]), "name": name,
                                     "backend": backend, "live": {"status": "waiting for mention"}}
                                    for name, backend in zip(names, backends))
                for i in range(1, 901):
                    rt.tick(st if i % 10 == 1 else None, AT + i * 100)
                view = rt.view()
                for name in names:
                    self.assertFalse(view["chars"][name]["hidden"], name)
                    self.assertFalse(view["chars"][name].get("setup"), name)
                    seat = next(s for s in view["seats"] if s and s["name"] == name)
                    self.assertTrue(seat["desk"] and seat["bed"], name)

    def test_saved_hires_recover_from_an_entry_exit_deadlock_without_resetting(self) -> None:
        st = state()
        rt = self.runtime(st)
        names = [f"hire-{i}" for i in range(3)]
        st["agents"].extend({**copy.deepcopy(st["agents"][0]), "name": name} for name in names)
        rt.tick(st, AT + 50)
        rt.ctx.eval('''const d=scene._debug(), [x,y]=d.DOOR;
          const outgoing=d.chars["hire-0"], incoming=d.chars["hire-1"];
          Object.assign(outgoing,{hidden:false,tx:x,ty:y+2,px:x*32,py:(y+2)*32,
            moving:false,steps:[[x,y+1],[x,y]],goal:[x,y],wait:0,stepAt:null});
          outgoing.setup=outgoing.setup.slice(2);
          Object.assign(incoming,{hidden:false,tx:x,ty:y+1,px:x*32,py:(y+1)*32,
            moving:false,steps:[[x,y+2]],goal:[x,y+2],wait:0});''')
        for i in range(2, 12):
            rt.tick(None, AT + i * 100)
        self.assertTrue(any(c.get("trafficYield") for c in rt.view()["chars"].values()),
                        "the old scene resolves the deadlock by walking into a passing bay")
        rt.save()
        before = rt.view()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=AT + 1100)
        self.assertEqual(restored.view(), before)
        self.assertFalse(before["seats"][-1]["desk"], "the pending furniture was not reset")
        previous = before
        for i in range(12, 912):
            restored.tick(st if i % 10 == 1 else None, AT + i * 100)
            current = restored.view()
            for name in names:
                a, b = previous["chars"][name], current["chars"][name]
                if not a["hidden"] and not b["hidden"]:
                    self.assertLessEqual(abs(b["px"] - a["px"]) + abs(b["py"] - a["py"]), 13,
                                         "recovery walks at normal speed without teleporting")
            previous = current
        view = restored.view()
        for name in names:
            self.assertFalse(view["chars"][name].get("setup"), name)
            self.assertFalse(view["chars"][name]["hidden"], name)
            seat = next(s for s in view["seats"] if s and s["name"] == name)
            self.assertTrue(seat["desk"] and seat["bed"], name)

    def test_saved_smoker_clears_the_entrance_for_a_new_hire(self) -> None:
        st = state("idle")
        rt = self.runtime(st)
        rt.ctx.eval('''const d=scene._debug(), c=d.chars.dev, [x,y]=d.DOOR;
          Object.assign(c,{hidden:false,tx:x,ty:y+2,px:x*32,py:(y+2)*32,
            moving:false,steps:[],mode:"smoke",sleepReady:true,
            smoke:{spot:[x,y+2],until:__officeClock+200000,nextLine:__officeClock+200000}});''')
        st["agents"].append({**copy.deepcopy(st["agents"][0]), "name": "hire"})
        rt.tick(st, AT + 100)
        self.assertNotEqual(rt.view()["chars"]["dev"]["smoke"]["spot"],
                            rt.ctx.execute('scene._debug().DOOR.map((v,i)=>v+(i?2:0))'))
        for i in range(2, 902):
            rt.tick(st if i % 10 == 1 else None, AT + i * 100)
        view = rt.view()
        self.assertFalse(view["chars"]["hire"].get("setup"))
        self.assertTrue(view["seats"][-1]["desk"] and view["seats"][-1]["bed"])

    def test_entrance_queue_does_not_reserve_an_immobile_casualtys_greeting_tile(self) -> None:
        st = state()
        rt = self.runtime(st)
        rt.ctx.eval('''const d=scene._debug(), c=d.chars.dev, [x,y]=d.DOOR;
          d.tune({despairShare:0});
          Object.assign(c,{tx:x,ty:y+2,px:x*32,py:(y+2)*32,moving:false,steps:[]});''')
        st["agents"][0]["live"]["status"] = "error"
        rt.tick(st, AT + 100)
        medics = [c for c in rt.view()["chars"].values() if c.get("medic")]
        self.assertEqual(len(medics), 2)
        self.assertFalse(rt.view()["chars"]["medic-a"]["hidden"],
                         "the introduction reservation must not hold the doctor outside")

    def test_compaction_coffee_work_and_event_scripts_are_authoritative(self) -> None:
        st = state("resuming")
        rt = self.runtime(st)
        self.assertEqual(rt.view()["chars"]["dev"]["mode"], "coffee")
        st["agents"][0]["live"]["status"] = "retrying"
        rt.tick(st, AT + 25)
        self.assertEqual(rt.view()["chars"]["dev"]["mode"], "coffee")
        st["agents"][0]["live"]["status"] = "working"
        rt.tick(st, AT + 50)
        self.assertEqual(rt.view()["chars"]["dev"]["mode"], "work")
        st["agents"][0]["info"]["compacting"] = True
        for i in range(1, 31):
            rt.tick(st, AT + i * 1000)
            self.assertEqual(rt.view()["chars"]["dev"]["mode"], "toilet")
        st["agents"][0]["info"]["compacting"] = False
        st["agents"][0]["info"]["compactions"] += 1
        at = AT + 31000
        st["events"] = [{"id": 100, "agent": "watchdog", "kind": "recovery", "detail": "Wake master",
                         "created_at": datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()}]
        rt.tick(st, at)
        self.assertTrue(rt.view()["guardQueue"] or rt.view()["chars"]["#guard"].get("setup"))
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=at)
        self.assertEqual(restored.view()["seenWatch"], rt.view()["seenWatch"])
        self.assertEqual(restored.view()["guardQueue"], rt.view()["guardQueue"])

    def test_guard_salutes_with_500_greetings_and_restores_exact_encounter(self) -> None:
        rt = self.runtime(state())
        greetings = rt.ctx.execute('scene._debug().GUARD_GREETINGS')
        self.assertEqual(len(greetings), 500)
        self.assertEqual(len(set(greetings)), 500)
        self.assertTrue(all(line.startswith('老板好') for line in greetings))
        rt.ctx.eval('''const g=scene._debug().chars["#guard"];
          g.hidden=false; g.setup=[{k:"gear",gear:"radio",ms:8000,text:"Actual watchdog message"}];
          g.stepAt=__officeClock; g.say="Actual watchdog message"; g.sayStart=__officeClock; g.sayUntil=__officeClock+8000;
          const b=scene._debug().chars["#boss"]={...g,name:"#boss",boss:true,guard:false,mode:"boss",setup:null,steps:[],moving:false};
          b.tx=g.tx+4; b.ty=g.ty; b.px=g.px+128; b.py=g.py;''')
        rt.tick(state(), AT + 50)
        guard = rt.view()['chars']['#guard']
        self.assertTrue(guard['saluting'])
        self.assertIn(guard['say'], greetings)
        self.assertTrue(rt.view()['animation_frames'][-1]['chars']['#guard'][17])
        rt.save()
        saved = rt.store.get_control('office_scene', '')
        restored = self.runtime(state(), saved=saved, now=AT + 50)
        self.assertEqual(restored.view(), rt.view())
        for i in range(2, 22):
            rt.tick(None, AT + i * 50)
        self.assertEqual(rt.view()['chars']['#guard']['say'], guard['say'])
        rt.ctx.execute('scene._debug().chars["#boss"].px += 1')
        rt.tick(None, AT + 1100)
        after = rt.view()['chars']['#guard']
        self.assertFalse(after['saluting'])
        self.assertEqual(after['say'], guard['say'], 'salute ends immediately but the greeting stays readable')
        self.assertGreaterEqual(after['sayUntil'] - after['sayStart'], 7500)
        rt.save()
        restored = self.runtime(state(), saved=rt.store.get_control('office_scene', ''), now=AT + 1100)
        self.assertEqual(restored.view(), rt.view(), 'refresh retains the unfinished greeting after the boss leaves')
        rt.tick(None, guard['sayUntil'] + 50)
        after = rt.view()['chars']['#guard']
        self.assertEqual(after['say'], 'Actual watchdog message')
        self.assertGreater(after['stepAt'], AT)

    def test_watchdog_dialogue_visits_actual_target_and_preserves_actual_reply(self) -> None:
        import json
        st = state()
        rt = self.runtime(st)
        for i in range(1, 4):
            rt.tick(st, AT + i * 50)
        at = AT + 200
        st['events'] = [{'id':80, 'agent':'watchdog', 'kind':'dialogue',
                         'detail':json.dumps({'target':'dev', 'speaker':'dev', 'text':'Actual saved session is ready.'}),
                         'created_at':datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()}]
        rt.tick(st, at)
        setup = rt.view()['chars']['#guard'].get('setup') or []
        queued = rt.view()['guardQueue']
        self.assertTrue(any(s.get('target') == 'dev' for s in setup) or any(q.get('target') == 'dev' for q in queued))
        for i in range(5, 600):
            rt.tick(None, AT + i * 50)
            if rt.view()['chars']['dev'].get('say') == 'Actual saved session is ready.':
                break
        else:
            self.fail('Guard never delivered the real target reply')

    def test_theme_migration_only_accepts_the_first_browser_preference(self) -> None:
        rt = self.runtime(state())
        rt.requested_theme = ""
        rt.store.set_control("office_theme", "")
        rt.set_theme("regular", initialize=True)
        self.assertEqual(rt.set_theme("chinese_tech", initialize=True)["requested_theme"], "regular")
        before = rt.view()
        with self.assertRaisesRegex(ValueError, "unknown Office theme"):
            rt.set_theme("not-a-theme")
        self.assertEqual(rt.view(), before)

    def test_thinking_pose_is_chosen_and_preserved_by_the_backend(self) -> None:
        st = state()
        st["agents"][0]["info"]["activity"]["kind"] = "thinking"
        rt = self.runtime(st)
        before = rt.view()
        self.assertIn(before["chars"]["dev"]["thinkStyle"], ("phone", "chin"))
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""))
        self.assertEqual(restored.view(), before)
        for i in range(1, 10):
            restored.tick(st, AT + i * 50)
            self.assertEqual(restored.view()["chars"]["dev"]["thinkStyle"], before["chars"]["dev"]["thinkStyle"])

    def test_retirement_shrinks_the_room_and_stays_compact_after_restart(self) -> None:
        st = state()
        st["agents"] += [{**copy.deepcopy(st["agents"][0]), "name": f"dev-{i}"} for i in range(8)]
        rt = self.runtime(st, theme="regular")
        capacity = rt.view()["capacity"]
        st["agents"] = st["agents"][:1]
        for i in range(1, 5001):
            rt.tick(st, AT + i * 100)
            if len(rt.scene["chars"]) == 2 and len(rt.scene["seats"]) == 1:
                break
        self.assertEqual(set(rt.scene["chars"]), {"dev", "#guard"})
        self.assertEqual(len(rt.scene["seats"]), 1)
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=rt.last_at, theme="regular")
        self.assertEqual(restored.view()["capacity"], 1)
        self.assertLess(restored.view()["capacity"], capacity)
        self.assertEqual(restored.view(), rt.view())

    def test_animation_history_covers_polling_gaps_and_stays_bounded(self) -> None:
        st = state("idle")
        rt = self.runtime(st)
        st["agents"][0]["live"]["status"] = "working"
        for i in range(1, 101):
            rt.tick(st, AT + i * 50)
        frames = rt.view()["animation_frames"]
        self.assertGreaterEqual(frames[-1]["at"] - frames[0]["at"], 2000)
        self.assertLessEqual(len(frames), 64)
        self.assertEqual(frames[-1]["at"], rt.scene["at"])
        self.assertEqual(frames[-1]["chars"]["dev"][:2], [rt.scene["chars"]["dev"]["px"], rt.scene["chars"]["dev"]["py"]])

    def test_full_messages_have_fixed_page_times_and_buffered_speech(self) -> None:
        rt = self.runtime(state())
        text = '完整内容🙂' * 200 + '最后一句全部说完。'
        rt.ctx.eval(f'scene._debug().setSpeech(scene._debug().chars.dev, {json.dumps(text)}, __officeClock + 1000)')
        rt.tick(None, AT + 50)
        view = rt.view()
        c = view['chars']['dev']
        self.assertEqual(c['say'], text)
        pages = rt.ctx.execute(f'scene._debug().pageLines({json.dumps(text)})')
        self.assertEqual(''.join(''.join(p) for p in pages), text)
        self.assertGreater(c['sayUntil'] - c['sayStart'], 45000, 'long speech must not be cut off by the old cap')
        for i, page in enumerate(pages):
            shown = rt.ctx.execute(f'scene._debug().bubblePage(scene._debug().chars.dev, {AT + i * 6000})')
            self.assertEqual(shown['lines'], page)
            still = rt.ctx.execute(f'scene._debug().bubblePage(scene._debug().chars.dev, {AT + i * 6000 + 5999})')
            self.assertEqual(still['lines'], page)
        self.assertIsNone(rt.ctx.execute(f'scene._debug().bubblePage(scene._debug().chars.dev, {AT - 1})'))
        self.assertIsNone(rt.ctx.execute('scene._debug().bubblePage({say:"   ",sayStart:0,sayUntil:1e15}, 1)'))
        for i in range(2, 32):
            rt.tick(None, AT + i * 50)
        view = rt.view()
        ids = {f['chars']['dev'][19] for f in view['animation_frames'] if f['chars']['dev'][19]}
        self.assertEqual(len(ids), 1, 'full utterance is stored once rather than duplicated in every animation frame')
        speech = view['animation_speech'][next(iter(ids))]
        self.assertEqual(speech['say'], text)
        rt.save()
        restored = self.runtime(state(), saved=rt.store.get_control('office_scene', ''), now=AT + 1550)
        self.assertEqual(restored.view(), rt.view(), 'speech page progress and frame references survive restart')

    def test_board_posts_queue_complete_text_in_order(self) -> None:
        st = state()
        rt = self.runtime(st, theme='regular')
        first, second = '完整长消息' * 180 + '第一条末尾。', '第二条必须等第一条全部显示。'
        st['events'] = [{'id': n, 'agent': 'dev', 'kind': 'comment', 'detail': '#1: short preview', 'speech': text,
                         'created_at': datetime.fromtimestamp((AT + 50) / 1000, timezone.utc).isoformat()}
                        for n, text in ((2, second), (1, first))]
        rt.tick(st, AT + 50)
        c = rt.view()['chars']['dev']
        self.assertEqual(c['say'], first)
        self.assertEqual([p['text'] for p in c['pendingPosts']], [second])
        for i in range(2, 30):
            rt.tick(st, AT + i * 50)
            self.assertEqual(rt.view()['chars']['dev']['say'], first)
        rt.tick(st, c['sayUntil'] + 1)
        self.assertEqual(rt.view()['chars']['dev']['say'], second)
        self.assertEqual(rt.view()['chars']['dev']['pendingPosts'], [])

    def test_queued_local_speech_after_visit_has_a_valid_destination(self) -> None:
        st = state()
        st['agents'].append({**copy.deepcopy(st['agents'][0]), 'name':'peer'})
        rt = self.runtime(st, theme='regular')
        rt.ctx.eval('''const c=scene._debug().chars.dev;
          c.mode="talk"; c.moving=false; c.steps=[]; c.setup=null;
          c.talk={kind:"visit",target:"peer",until:__officeClock-1};
          c.say="Previous conversation"; c.sayStart=__officeClock-8000; c.sayUntil=__officeClock-1;
          c.pendingPosts=[{id:2,text:"Next reply is spoken here, without a mention."}];''')
        rt.tick(None, AT + 50)
        c = rt.view()['chars']['dev']
        self.assertEqual(c['talk']['kind'], 'here')
        self.assertEqual(c['mode'], 'work')
        self.assertEqual(c['say'], 'Next reply is spoken here, without a mention.')
        self.assertEqual(c['goal'], rt.ctx.execute('scene._debug().DESKS[scene._debug().chars.dev.idx].chair'))
        for i in range(2, 100):
            rt.tick(None, AT + i * 50)
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control('office_scene', ''), now=AT + 4950, theme='regular')
        for i in range(100, 140):
            restored.tick(None, AT + i * 50)
        self.assertEqual(restored.view()['chars']['dev']['say'], c['say'])

    def test_medics_replace_stale_patient_paths_when_leaving_after_resume(self) -> None:
        st = state('error')
        rt = self.runtime(st, theme='chinese_tech')
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR;
          d.chars.dev.mode="faint";d.stepChars(0);
          for(const [name,offset] of [["medic-a",2],["medic-b",1]]) {
            const c=d.chars[name];
            Object.assign(c,{hidden:false,tx:x,ty:y+offset,px:x*32,py:(y+offset)*32,
              moving:false,setup:[{k:"exit"},{k:"gone"}],
              goal:name==="medic-a"?[x,y]:[1,4],
              steps:name==="medic-a"?[[x,y+1],[x,y]]:[[x-1,y+1],[1,4]],treat:null});
          }
          Object.assign(d.chars["#guard"],{hidden:false,tx:x-1,ty:y+1,px:(x-1)*32,py:(y+1)*32,
            moving:false,setup:null,steps:[],mode:"guard"});''')
        st['agents'][0]['live']['status'] = 'resuming'
        rt.tick(st, AT + 50)
        rt.save()
        # Existing scenes must recover without dropping or teleporting actors.
        rt = self.runtime(st, saved=rt.store.get_control('office_scene',''), now=AT+50, theme='chinese_tech')
        for i in range(2, 302):
            rt.tick(None, AT + i * 50)
        self.assertFalse(any(c.get('medic') for c in rt.view()['chars'].values()))
        self.assertIsNone(rt.view()['medics'])
        self.assertEqual(rt.view()['chars']['dev']['mode'], 'coffee')

    def test_new_mode_supplies_the_destination_before_pathfinding(self) -> None:
        st = state('working')
        rt = self.runtime(st, theme='regular')
        rt.ctx.eval('''const c=scene._debug().chars.dev;
          c.mode="coffee"; c.moving=false; c.steps=[]; c.cup=0;
          c.cupAt=__officeClock-5000;''')
        rt.tick(None, AT + 50)
        c = rt.view()['chars']['dev']
        self.assertEqual(c['mode'], 'work')
        self.assertEqual(c['goal'], rt.ctx.execute('scene._debug().DESKS[scene._debug().chars.dev.idx].chair'))

    def test_office_resolves_exact_full_messages_and_migrates_existing_stores(self) -> None:
        path = Path(self.tmp.name) / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute("INSERT INTO events(agent,kind,detail,created_at) VALUES('dev','comment','legacy preview','2025-01-01')")
        store = Store(path)
        self.addCleanup(store.close)
        body = '这里是完整正文🙂' * 250 + '最后的验收结论。'
        thread = store.create_thread('human', '完整主题', body)
        replies = [store.add_comment(thread['id'], 'dev', text) for text in (body, body + '第二条')]
        events = store.office_events()
        self.assertEqual(events[0]['ref_id'], replies[1]['id'])
        self.assertEqual(events[0]['speech'], replies[1]['body'])
        self.assertEqual(events[1]['speech'], replies[0]['body'])
        self.assertEqual(events[2]['speech'], '完整主题\n' + body)
        self.assertLess(len(events[0]['detail']), 150, 'discussion/activity previews remain bounded')
        self.assertIsNone(events[3]['speech'], 'legacy events remain readable')

    def test_boss_and_watchdog_keep_the_whole_utterance(self) -> None:
        st = state()
        rt = self.runtime(st)
        text = '需要逐页显示的完整内容。' * 90 + '最终操作要求。'
        at = AT + 50
        st['events'] = [{'id':1, 'agent':'human', 'kind':'comment', 'detail':'#1: truncated', 'speech':text,
                         'created_at':datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()}]
        rt.tick(st, at)
        boss = rt.view()['chars']['#boss']
        relay = next(step for step in boss['setup'] if step['k'] == 'relay')
        self.assertTrue(relay['text'].endswith(text))
        thread = rt.store.create_thread('local-watchdog', 'Talk', '@dev ' + text)
        rt.store.add_comment(thread['id'], 'dev', text)
        dialogue = next(json.loads(e['detail']) for e in rt.store.list_events() if e['kind'] == 'dialogue')
        self.assertEqual(dialogue['text'], text)

    def test_boss_tags_visit_only_the_named_agents_and_keep_500_acknowledgements(self) -> None:
        st = state()
        st['agents'].extend([{**copy.deepcopy(st['agents'][0]), 'name':'master', 'role':'master'},
                             {**copy.deepcopy(st['agents'][0]), 'name':'qa', 'role':'qa'}])
        rt = self.runtime(st)
        bank = json.loads(rt.ctx.eval('JSON.stringify(scene._debug().CN_OBEY)'))
        self.assertEqual(len(bank),500)
        self.assertEqual(len(set(bank)),500)
        for text, targets in (('@dev 请处理问题。',['dev']),('@qa @dev 两位处理。',['qa','dev']),('请主管安排。',['master'])):
            script = json.loads(rt.ctx.eval('JSON.stringify(scene._debug().bossScript(' + json.dumps({'text':text,'targets':[n for n in targets if '@'+n in text]}) + '))'))
            relays = [step for step in script if step['k']=='relay']
            self.assertEqual([step['target'] for step in relays],targets)
            self.assertEqual([step['listeners'] for step in relays],[[n] for n in targets])
            self.assertTrue(all(not step['megaphone'] for step in relays))
        all_script = json.loads(rt.ctx.eval('JSON.stringify(scene._debug().bossScript({text:"@all 开会",all:true,targets:[]}))'))
        self.assertEqual(len([s for s in all_script if s['k']=='relay']),1)
        self.assertTrue(next(s for s in all_script if s['k']=='relay')['megaphone'])

    def test_targeted_visit_stops_smoking_yields_and_finishes_the_saved_reply(self) -> None:
        st = state('waiting for mention')
        st['agents'].append({**copy.deepcopy(st['agents'][0]), 'name':'master', 'role':'master'})
        rt = self.runtime(st)
        rt.ctx.eval('''const d=scene._debug(),c=d.chars.dev;
          c.tx=d.DOOR[0];c.ty=d.DOOR[1]+1;c.px=c.tx*32;c.py=c.ty*32;
          c.hidden=false;c.moving=false;c.steps=[];c.setup=null;c.mode="smoke";
          c.smoke={until:__officeClock+999999,spot:[c.tx,c.ty],nextLineAt:0};''')
        st['events']=[{'id':701,'agent':'human','kind':'comment','detail':'#1: @dev 处理。','speech':'@dev 处理。',
                       'created_at':datetime.fromtimestamp((AT+50)/1000,timezone.utc).isoformat()}]
        rt.tick(st,AT+50)
        c=rt.view()['chars']['dev']
        self.assertIsNone(c['smoke'])
        self.assertIsNotNone(c['bossYield'])
        door=rt.ctx.eval('scene._debug().DOOR[0]')
        self.assertGreater(abs(c['bossYield']['spot'][0]-door),1)
        ack = None
        for i in range(2,1600):
            rt.tick(st,AT+i*50)
            c=rt.view()['chars']['dev']
            if c.get('say','').startswith('收到！'):
                ack=c
                break
        self.assertIsNotNone(ack,'the boss must enter, find @dev, then receive its reply')
        self.assertGreaterEqual(ack['sayUntil']-ack['sayStart'],7500)
        self.assertFalse(rt.view()['chars']['master'].get('say','').startswith('收到！'))
        rt.save()
        restored=self.runtime(st,saved=rt.store.get_control('office_scene',''),now=AT+i*50)
        self.assertEqual(restored.view(),rt.view(),'reply and particle phases survive a restart')
        for j in range(i+1,i+25):
            rt.tick(st,AT+j*50)
            restored.tick(st,AT+j*50)
            self.assertEqual(restored.view(),rt.view())
            self.assertEqual(rt.view()['chars']['dev']['say'],ack['say'])
        plan=rt.view()['chars']['#boss']['setup'][0]
        self.assertEqual(plan['k'],'acks')
        self.assertGreaterEqual(plan['end'],ack['sayUntil'])

    def test_boss_crosses_the_door_before_disappearing_and_keeps_buffered_appearance(self) -> None:
        st = state()
        rt = self.runtime(st)
        st['events'] = [{'id':1, 'agent':'human', 'kind':'comment', 'speech':'老板说完了。', 'detail':'#1: goodbye',
                         'created_at':datetime.fromtimestamp((AT + 50) / 1000, timezone.utc).isoformat()}]
        rt.tick(st, AT + 50)
        rt.ctx.eval('''const d=scene._debug(), b=d.chars["#boss"];
          b.hidden=false; b.tx=d.DOOR[0]; b.ty=d.DOOR[1]+3; b.px=b.tx*32; b.py=b.ty*32;
          b.setup=[{k:"exit"},{k:"gone"}]; b.moving=false; b.steps=[];''')
        visible = []
        for i in range(2, 101):
            rt.tick(None, AT + i * 50)
            view = rt.view()
            boss = view['chars'].get('#boss')
            if not boss:
                break
            if not boss['hidden']:
                visible.append(boss)
        else:
            self.fail('Boss never finished walking out')
        door = rt.ctx.execute('scene._debug().DOOR')
        crossing = [b for b in visible if b['py'] < door[1] * 32]
        self.assertGreaterEqual(len(crossing), 4, 'show multiple walking frames past the threshold')
        self.assertTrue(all(b['dir'] == 'up' for b in crossing))
        self.assertEqual(crossing[-1]['py'], (door[1] - 1) * 32)
        self.assertIn('#boss', view['animation_characters'], 'the removed boss must remain available to buffered rendering')
        self.assertNotIn('#boss', view['chars'], 'the backend can finish the visit without waiting for a browser')
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control('office_scene', ''), now=AT + i * 50)
        self.assertEqual(restored.view()['animation_characters'], view['animation_characters'])
        self.assertEqual(restored.view()['animation_frames'], view['animation_frames'])
        for n in range(i + 1, i + 61):
            rt.tick(None, AT + n * 50)
        self.assertNotIn('#boss', rt.view()['animation_characters'], 'release appearance after its history expires')

    def test_twenty_minutes_of_simulation_does_not_exhaust_v8_memory(self) -> None:
        st = state("idle")
        st["agents"] += [{**copy.deepcopy(st["agents"][0]), "name": f"dev-{i}"} for i in range(8)]
        rt = self.runtime(st)
        for i in range(1, 24001):
            rt.tick(st if i % 5 == 0 else None, AT + i * 50)
        self.assertFalse(rt.ctx.was_hard_memory_limit_reached())
        self.assertLess(rt.ctx.heap_stats()["total_physical_size"], 64 * 1024 * 1024)
        self.assertGreaterEqual(rt.scene["revision"], 24000)


if __name__ == "__main__":
    unittest.main()
