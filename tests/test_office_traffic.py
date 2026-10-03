"""Office right-of-way, including saved salute stalls and crowded corridors."""
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from huntun.office import OfficeRuntime
from huntun.store import Store
from tests.test_office_state import AT, state


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = 0

    def runtime(self, st, *, theme="chinese_tech", saved="", now=AT):
        self.index += 1
        store = Store(Path(self.tmp.name) / f"scene-{self.index}.db")
        self.addCleanup(store.close)
        store.set_control("office_theme", theme)
        if saved:
            store.set_control("office_scene", saved)
        rt = OfficeRuntime(store, st, seed=1, now=now)
        self.addCleanup(rt.close)
        return rt

    def boss(self, rt, st):
        st["events"] = [{"id":1, "agent":"human", "kind":"comment", "speech":"@dev 请继续。",
                         "detail":"#1: message", "created_at":datetime.fromtimestamp((AT+50)/1000,timezone.utc).isoformat()}]
        rt.tick(st, AT+50)

    def test_saved_mid_step_salute_cannot_reserve_the_bosss_exit_forever(self):
        st = state()
        rt = self.runtime(st)
        self.boss(rt, st)
        rt.ctx.eval('''const d=scene._debug(), [x,y]=d.DOOR, g=d.chars["#guard"], b=d.chars["#boss"];
          Object.assign(g,{hidden:false,tx:x-1,ty:y+1,nx:x,ny:y+1,prog:.72,
            px:(x-.28)*32,py:(y+1)*32,moving:true,steps:[[x,y+2],[x,y+3]],
            goal:[x,y+3],setup:[{k:"goto",where:"guardAhead"},{k:"gear",gear:"radio",ms:8000,text:"Actual watchdog task"}]});
          Object.assign(b,{hidden:false,tx:x,ty:y+2,px:x*32,py:(y+2)*32,moving:false,
            steps:[[x,y+1],[x,y]],goal:[x,y],setup:[{k:"exit"},{k:"gone"}]});''')
        rt.tick(None, AT+100)
        rt.save()
        before = rt.view()
        self.assertTrue(before["chars"]["#guard"]["saluting"])
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=AT+100)
        self.assertEqual(restored.view(), before)
        previous = before
        for i in range(2, 202):
            restored.tick(None, AT+i*100)
            view = restored.view()
            for name in ("#guard", "#boss"):
                a, b = previous["chars"].get(name), view["chars"].get(name)
                if a and b and not a["hidden"] and not b["hidden"]:
                    self.assertLessEqual(abs(a["px"]-b["px"])+abs(a["py"]-b["py"]),16)
            previous = view
        self.assertNotIn("#boss", restored.view()["chars"], "the boss must walk through the door and leave")
        self.assertIn("#guard", restored.view()["chars"])
        self.assertIn("Actual watchdog task", str(restored.view()["chars"]["#guard"]), "yielding retains the real watchdog script")

    def test_guard_acknowledges_the_boss_without_waiting_for_the_boss_to_leave(self):
        st = state()
        rt = self.runtime(st)
        self.boss(rt, st)
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR,b=d.chars["#boss"],g=d.chars["#guard"];
          Object.assign(g,{hidden:false,moving:false,steps:[],setup:null});
          Object.assign(b,{hidden:false,tx:x,ty:y+2,px:x*32,py:(y+2)*32,moving:false,steps:[],stepAt:null,
            setup:[{k:"acks",listeners:["#guard"]},{k:"exit"},{k:"gone"}]});''')
        acknowledged = False
        for i in range(1, 601):
            rt.tick(None, AT+50+i*100)
            acknowledged |= rt.view()["chars"]["#guard"].get("say", "").startswith("收到！")
        self.assertTrue(acknowledged)
        self.assertNotIn("#boss", rt.view()["chars"])

    def test_hidden_hires_can_enter_past_a_stationary_guard_in_every_theme(self):
        themes = self.runtime(state()).themes
        for theme in themes:
            with self.subTest(theme=theme):
                st = state()
                rt = self.runtime(st, theme=theme)
                rt.ctx.eval('''const d=scene._debug(),g=d.chars["#guard"],[x,y]=d.DOOR;
                  Object.assign(g,{hidden:false,tx:x,ty:y+1,px:x*32,py:(y+1)*32,
                    moving:false,steps:[],goal:[x,y+1],setup:[{k:"gear",gear:"radio",ms:60000,text:"Watchdog busy"}],stepAt:null});''')
                st["agents"].extend({**copy.deepcopy(st["agents"][0]), "name": f"hire-{i}", "backend": b}
                    for i,b in enumerate(("codex", "claude-code", "pi-clm", "api")))
                for i in range(1, 1001):
                    rt.tick(st if i%10 == 1 else None, AT+i*100)
                view = rt.view()
                for i in range(4):
                    c = view["chars"][f"hire-{i}"]
                    self.assertFalse(c["hidden"])
                    self.assertFalse(c.get("setup"), f"{theme}: hire-{i} finished moving in")
                    seat = next(s for s in view["seats"] if s and s["name"] == f"hire-{i}")
                    self.assertTrue(seat["desk"] and seat["bed"])

    def test_both_medics_reach_a_doorway_casualty_and_leave_after_recovery(self):
        st = state()
        rt = self.runtime(st)
        rt.ctx.eval('''const d=scene._debug(),c=d.chars.dev,[x,y]=d.DOOR;
          d.tune({despairShare:0});
          Object.assign(c,{tx:x,ty:y+2,px:x*32,py:(y+2)*32,moving:false,steps:[]});
          const g=d.chars["#guard"];
          Object.assign(g,{tx:x-1,ty:y+1,px:(x-1)*32,py:(y+1)*32,
            moving:false,steps:[],setup:[{k:"gear",gear:"radio",ms:60000,text:"Watchdog busy"}],stepAt:null});''')
        st["agents"][0]["live"]["status"] = "error"
        treated = set()
        for i in range(1, 401):
            rt.tick(st if i%10 == 1 else None, AT+i*100)
            treated.update(c["name"] for c in rt.view()["chars"].values() if c.get("treat"))
        self.assertEqual(treated, {"medic-a", "medic-b"})
        st["agents"][0]["live"]["status"] = "working"
        for i in range(401, 1001):
            rt.tick(st if i%10 == 1 else None, AT+i*100)
        self.assertFalse(any(c.get("medic") for c in rt.view()["chars"].values()))
        self.assertIsNone(rt.view()["medics"])

    def test_medics_can_reach_a_casualty_on_the_door_or_entry(self):
        for dy in (0,1):
            with self.subTest(door_offset=dy):
                st=state()
                rt=self.runtime(st)
                rt.ctx.eval(f'''const d=scene._debug(),[x,y]=d.DOOR,c=d.chars.dev;
                  d.tune({{despairShare:0}});
                  Object.assign(c,{{tx:x,ty:y+{dy},px:x*32,py:(y+{dy})*32,moving:false,steps:[],goal:[x,y+{dy}]}});''')
                rt.tick(None,AT+50)
                st['agents'][0]['live']['status']='error'
                treated=False
                for i in range(1,101):
                    rt.tick(st if i==1 else None,AT+i*100)
                    chars=rt.view()['chars']
                    for c in chars.values():
                        if c.get('treat'):
                            p=chars['dev']
                            treated=True
                            self.assertEqual(abs(c['tx']-p['tx'])+abs(c['ty']-p['ty']),1)
                            self.assertFalse(c['moving'])
                self.assertTrue(treated,'a blocked doorway must not keep doctors out')
                rt.save()
                restored=self.runtime(st,saved=rt.store.get_control('office_scene',''),now=rt.last_at)
                self.assertEqual(restored.view(),rt.view())
                st['agents'][0]['live']['status']='working'
                for i in range(101,301):
                    restored.tick(st if i==101 else None,AT+i*100)
                self.assertIsNone(restored.view()['medics'])

    def test_saved_medics_follow_the_patient_instead_of_treating_an_old_position(self):
        st = state()
        rt = self.runtime(st)
        rt.ctx.eval('scene._debug().tune({despairShare:0})')
        st["agents"][0]["live"]["status"] = "error"
        rt.tick(st, AT+100)
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR,p=d.chars.dev;
          Object.assign(p,{tx:x-3,ty:y+2,px:(x-3)*32,py:(y+2)*32,moving:false,steps:[],goal:[x-3,y+2]});
          for(const [n,offset] of [["medic-a",2],["medic-b",3]]) {
            const c=d.chars[n];
            Object.assign(c,{hidden:false,tx:x,ty:y+offset,px:x*32,py:(y+offset)*32,moving:false,
              goal:[x,y+offset],steps:[],treat:{patient:"dev",since:__officeClock,nextLine:__officeClock+1000},
              setup:[{k:"treat",patient:"dev"},{k:"nextPatient"}]});
          }''')
        rt.tick(None, AT+150)
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=rt.last_at)
        for i in range(2, 202):
            restored.tick(None, AT+i*100)
            chars = restored.view()["chars"]
            for name in ("medic-a", "medic-b"):
                c,p=chars[name],chars["dev"]
                if c.get("treat"):
                    self.assertFalse(c["moving"])
                    self.assertEqual(abs(c["tx"]-p["tx"])+abs(c["ty"]-p["ty"]),1,
                                     "a medic may only treat beside the current patient")
        self.assertTrue(all(restored.view()["chars"][n].get("treat") for n in ("medic-a","medic-b")))

    def test_a_failure_clears_an_old_walk_after_finishing_the_current_step(self):
        st=state("resuming")
        rt=self.runtime(st)
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR,c=d.chars.dev;
          d.tune({despairShare:0});
          Object.assign(c,{tx:x,ty:y+1,nx:x,ny:y+2,px:x*32,py:(y+1.4)*32,moving:true,prog:.4,
            goal:[x,y+5],steps:[[x,y+3],[x,y+4],[x,y+5]]});''')
        rt.tick(None, AT+50)
        st["agents"][0]["live"]["status"]="error"
        previous=rt.view()["chars"]["dev"]
        for i in range(1,101):
            rt.tick(st if i==1 else None,AT+i*100)
            c=rt.view()["chars"]["dev"]
            self.assertLessEqual(abs(c["px"]-previous["px"])+abs(c["py"]-previous["py"]),13)
            self.assertEqual(c["mode"],"faint")
            self.assertFalse(c["steps"],"a casualty must not continue its coffee or work route")
            previous=c
        door=rt.ctx.execute('scene._debug().DOOR')
        self.assertEqual([c["tx"],c["ty"]],[door[0],door[1]+2])

    def test_an_error_overrides_listening_yielding_and_new_hire_scripts(self):
        for activity in ("listen","yield","setup"):
            with self.subTest(activity=activity):
                st=state()
                rt=self.runtime(st)
                rt.ctx.eval('scene._debug().tune({despairShare:0})')
                if activity=="listen":
                    rt.ctx.eval('scene._debug().chars.dev.hold={kind:"listen",until:__officeClock+60000}')
                elif activity=="yield":
                    rt.ctx.eval('''const c=scene._debug().chars.dev;
                      c.mode="yield";c.trafficYield={origin:[c.tx,c.ty],goal:[c.tx,c.ty],spot:[c.tx-1,c.ty],
                        mode:"work",resumeWalk:true,rejoin:false,by:"#guard",since:__officeClock,until:__officeClock+30000,phase:"aside"};''')
                else:
                    rt.ctx.eval('scene._debug().chars.dev.setup=[{k:"say",text:"Still installing furniture",ms:8000}]')
                st["agents"][0]["live"]["status"]="error"
                rt.tick(st,AT+100)
                c=rt.view()["chars"]["dev"]
                self.assertEqual(c["mode"],"faint")
                self.assertFalse(c.get("hold"))
                self.assertFalse(c.get("trafficYield"))
                for i in range(2,101):
                    rt.tick(None,AT+i*100)
                self.assertIsNotNone(rt.view()["medics"])
                if activity=="setup":
                    self.assertEqual(rt.view()["chars"]["dev"]["setup"][0]["k"],"say")
                    st["agents"][0]["live"]["status"]="working"
                    rt.tick(st,AT+10100)
                    self.assertEqual(rt.view()["chars"]["dev"]["setup"][0]["k"],"say","recovery resumes the interrupted script")
                    for i in range(102,221):
                        rt.tick(None,AT+i*100)
                    self.assertFalse(rt.view()["chars"]["dev"].get("setup"))

    def test_chinese_despair_keeps_both_poses_without_summoning_medics(self):
        for pose in ("desk","wall"):
            with self.subTest(pose=pose):
                st=state()
                rt=self.runtime(st)
                rt.ctx.eval(f'scene._debug().tune({{despairShare:1,despairPose:"{pose}"}})')
                st["agents"][0]["live"]["status"]="error"
                for i in range(1,101):
                    rt.tick(st if i%10==1 else None,AT+i*100)
                view=rt.view()
                self.assertEqual(view["chars"]["dev"]["mode"],"despair")
                self.assertEqual(view["chars"]["dev"]["despair"]["pose"],pose)
                self.assertIsNone(view["medics"])
                rt.save()
                restored=self.runtime(st,saved=rt.store.get_control("office_scene",""),now=rt.last_at)
                self.assertEqual(restored.view(),view,"restart preserves the original error branch")

    def test_an_agent_in_despair_can_still_clear_another_walkers_route(self):
        st=state()
        rt=self.runtime(st)
        rt.ctx.eval('scene._debug().tune({despairShare:1,despairPose:"wall"})')
        st['agents'][0]['live']['status']='error'
        rt.tick(st,AT+100)
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR,p=d.chars.dev;
          Object.assign(p,{tx:x-2,ty:y+3,px:(x-2)*32,py:(y+3)*32,moving:false,steps:[],goal:[x-2,y+3]});
          p.despair.spot=[x-2,y+3];
          d.chars.visitor={...p,name:"visitor",idx:99,failed:false,errorAt:null,despair:null,mode:"idle",hidden:false,
            tx:x-2,ty:y+4,px:(x-2)*32,py:(y+4)*32,moving:false,setup:null,
            goal:[x-2,y+3],steps:[[x-2,y+3]],hold:null,smoke:null};''')
        yielded=arrived=False
        for i in range(2,152):
            rt.tick(None,AT+i*100)
            chars=rt.view()['chars']
            yielded |= bool(chars['dev'].get('trafficYield'))
            v=chars['visitor']
            arrived |= [v['tx'],v['ty']]==v['goal'] and not v['moving']
        self.assertTrue(yielded)
        self.assertTrue(arrived,'error animation must not cancel a passing-bay lease every tick')
        self.assertIsNone(rt.view()['medics'])

    def test_a_treating_medic_can_yield_and_rejoin_without_treating_remotely(self):
        st=state()
        rt=self.runtime(st)
        rt.ctx.eval('scene._debug().tune({despairShare:0})')
        st["agents"][0]["live"]["status"]="error"
        rt.tick(st,AT+100)
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR,p=d.chars.dev;
          Object.assign(p,{tx:x-3,ty:y+2,px:(x-3)*32,py:(y+2)*32,moving:false,steps:[],goal:[x-3,y+2]});
          for(const [n,dx] of [["medic-a",-2],["medic-b",-4]]){
            const c=d.chars[n];
            Object.assign(c,{hidden:false,tx:x+dx,ty:y+2,px:(x+dx)*32,py:(y+2)*32,moving:false,steps:[],
              goal:[x+dx,y+2],treat:{patient:"dev",since:__officeClock,nextLine:__officeClock+1000},
              setup:[{k:"treat",patient:"dev"},{k:"nextPatient"}]});
          }
          d.chars.visitor={...p,name:"visitor",idx:99,failed:false,errorAt:null,mode:"idle",hidden:false,
            tx:x-2,ty:y+3,px:(x-2)*32,py:(y+3)*32,moving:false,setup:null,
            goal:[x-2,y+2],steps:[[x-2,y+2]],hold:null,smoke:null};''')
        yielded=False
        arrived=False
        for i in range(2,202):
            rt.tick(None,AT+i*100)
            chars=rt.view()["chars"]
            yielded |= bool(chars["medic-a"].get("trafficYield"))
            c=chars["visitor"]
            arrived |= [c["tx"],c["ty"]] == c["goal"] and not c["moving"]
            for n in ("medic-a","medic-b"):
                c=chars[n]
                if c.get("treat"):
                    p=chars[c["treat"]["patient"]]
                    self.assertFalse(c["moving"])
                    self.assertEqual(abs(c["tx"]-p["tx"])+abs(c["ty"]-p["ty"]),1)
        self.assertTrue(yielded)
        self.assertTrue(arrived)
        self.assertTrue(chars["medic-a"].get("treat"),"the doctor resumes care after giving way")

    def corridor(self, rt):
        rt.ctx.eval('''const d=scene._debug(), template=d.chars.dev;
          for(const c of Object.values(d.chars)) c.hidden=true;
          for(let y=0;y<d.MH;y++) for(let x=0;x<d.MW;x++) d.solid[y][x]=true;
          const cells=[]; for(let x=3;x<=9;x++) cells.push([x,8]);
          cells.push([3,9],[6,9],[6,10],[9,9]);
          for(const [x,y] of cells){d.solid[y][x]=false;d.map[y][x]=["carpet"];}
          function actor(name,x,y,goal){
            const c=d.chars[name]={...template,name,idx:99,hidden:false,tx:x,ty:y,px:x*32,py:y*32,
              moving:false,setup:null,mode:"idle",steps:[],goal:goal||[x,y],wait:0,hold:null,smoke:null};
            if(goal){const dx=Math.sign(goal[0]-x);for(let p=x+dx;p!==goal[0]+dx;p+=dx)c.steps.push([p,y]);}
          }
          actor("east",3,8,[9,8]);actor("west",9,8,[3,8]);actor("standing",5,8);actor("bay-occupant",6,9);
          d.chars["bay-occupant"].hold={kind:"listen",until:__officeClock+150000};''')

    def test_opposing_walkers_and_a_chain_of_stationary_listeners_clear_a_narrow_corridor(self):
        rt = self.runtime(state())
        self.corridor(rt)
        reached = set()
        for i in range(1, 601):
            rt.tick(None, AT+i*100)
            for name, target in (("east", [9,8]), ("west", [3,8])):
                c = rt.view()["chars"][name]
                if not c["moving"] and [c["tx"],c["ty"]] == target:
                    reached.add(name)
            visible = [c for c in rt.view()["chars"].values() if not c["hidden"]]
            reservations = set()
            for c in visible:
                tiles = {(c["tx"],c["ty"])}
                if c["moving"]:
                    tiles.add((c["nx"],c["ny"]))
                self.assertTrue(reservations.isdisjoint(tiles), "yielding cannot overlap actors or their next steps")
                reservations.update(tiles)
        chars = rt.view()["chars"]
        self.assertEqual(reached, {"east", "west"}, "both walkers complete their original trip")
        self.assertFalse(any(c.get("trafficYield") or c["steps"] for c in chars.values() if c["name"] in {"east", "west"}))
        self.assertEqual(chars["bay-occupant"]["hold"]["kind"],"listen")

    def test_a_saved_yield_keeps_the_script_and_completes_the_original_exit(self):
        st = state()
        rt = self.runtime(st)
        self.boss(rt, st)
        rt.ctx.eval('''const d=scene._debug(),[x,y]=d.DOOR,b=d.chars["#boss"],g=d.chars["#guard"];
          Object.assign(g,{hidden:false,tx:x,ty:y+1,px:x*32,py:(y+1)*32,moving:false,steps:[],
            goal:[x,y+1],setup:[{k:"gear",gear:"radio",ms:8000,text:"Saved watchdog task"}]});
          Object.assign(b,{hidden:false,tx:x,ty:y+2,px:x*32,py:(y+2)*32,moving:false,
            steps:[[x,y+1],[x,y]],goal:[x,y],setup:[{k:"exit"},{k:"gone"}]});''')
        for i in range(1, 11):
            rt.tick(None, AT+50+i*100)
        self.assertTrue(rt.view()["chars"]["#guard"].get("trafficYield"))
        rt.save()
        restored = self.runtime(st, saved=rt.store.get_control("office_scene", ""), now=rt.last_at)
        self.assertEqual(restored.view(), rt.view())
        for i in range(11, 201):
            rt.tick(None, AT+50+i*100)
            restored.tick(None, AT+50+i*100)
            self.assertEqual(restored.view(), rt.view(), "yielding is backend-owned and deterministic across restart")
        self.assertNotIn("#boss", restored.view()["chars"])

    def test_three_walkers_break_a_circular_wait_without_overlapping(self):
        rt = self.runtime(state(), theme="regular")
        rt.ctx.eval('''const d=scene._debug(),template=d.chars.dev;
          for(const c of Object.values(d.chars)) c.hidden=true;
          for(let y=0;y<d.MH;y++) for(let x=0;x<d.MW;x++) d.solid[y][x]=true;
          for(const [x,y] of [[4,8],[5,8],[5,9],[4,9]]) {d.solid[y][x]=false;d.map[y][x]=["carpet"];}
          for(const [name,at,to,steps] of [
            ["a",[4,8],[5,8],[[5,8]]],
            ["b",[5,8],[5,9],[[5,9]]],
            ["c",[5,9],[4,8],[[5,8],[4,8]]]]) {
            d.chars[name]={...template,name,idx:99,hidden:false,tx:at[0],ty:at[1],px:at[0]*32,py:at[1]*32,
              moving:false,setup:null,mode:"idle",steps,goal:to,hold:null,smoke:null,wait:0};
          }''')
        reached = set()
        for i in range(1, 401):
            rt.tick(None, AT+i*100)
            chars = rt.view()["chars"]
            reserved = set()
            for name, target in (("a", [5,8]), ("b", [5,9]), ("c", [4,8])):
                c = chars[name]
                tiles = {(c["tx"],c["ty"])}
                if c["moving"]:
                    tiles.add((c["nx"],c["ny"]))
                self.assertTrue(reserved.isdisjoint(tiles))
                reserved.update(tiles)
                if not c["moving"] and [c["tx"],c["ty"]] == target:
                    reached.add(name)
        self.assertEqual(reached, {"a", "b", "c"})
        self.assertFalse(any(chars[n].get("trafficYield") or chars[n]["steps"] for n in ("a","b","c")))
