"""Minimum room area, compact seats and safe migration of existing offices."""
import copy
import json
import math
import tempfile
import unittest
from collections import deque
from pathlib import Path

from huntun.office import OfficeRuntime
from huntun.store import Store
from tests.test_office_state import AT, state


def team(count, master=0):
    st=state()
    agent=st['agents'][0]
    st['agents']=[{**copy.deepcopy(agent),'name':f'agent-{i}','role':'master' if i==master else 'backend'} for i in range(count)]
    return st


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.serial=0

    def runtime(self,st,theme='regular',saved='',now=AT):
        self.serial+=1
        store=Store(Path(self.tmp.name)/f'{self.serial}.db')
        self.addCleanup(store.close)
        store.set_control('office_theme',theme)
        if saved:store.set_control('office_scene',saved)
        rt=OfficeRuntime(store,st,seed=1,now=now)
        self.addCleanup(rt.close)
        return rt

    def geometry(self,rt):
        return rt.ctx.execute('(()=>{const d=scene._debug();return {width:d.MW,height:d.MH,desks:d.DESKS,beds:d.BEDS,solid:d.solid,door:d.DOOR,toilets:d.TOILETS,printer:d.PRINT_SPOT};})()')

    def assert_access(self,geometry,count):
        width,height=geometry['width'],geometry['height']
        start=(geometry['door'][0],geometry['door'][1]+1)
        queue=deque([start]);seen={start}
        while queue:
            x,y=queue.popleft()
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
                p=(x+dx,y+dy)
                if 0<=p[0]<width and 0<=p[1]<height and not geometry['solid'][p[1]][p[0]] and p not in seen:
                    seen.add(p);queue.append(p)
        self.assertEqual(len(geometry['desks']),count)
        self.assertEqual(len(geometry['beds']),count)
        points=[d['chair'] for d in geometry['desks']]+geometry['beds']+[geometry['printer']]
        self.assertEqual(len(set(map(tuple,geometry['beds']))),count)
        for p in points:
            self.assertIn(tuple(p),seen,f'unreachable workstation or bed: {p}')
        for x,y in geometry['toilets']:
            self.assertTrue(any((x+dx,y+dy) in seen for dx,dy in ((1,0),(-1,0),(0,1),(0,-1))))

    def test_all_themes_use_minimum_area_and_keep_every_workstation_reachable(self):
        for theme in self.runtime(team(1)).themes:
            with self.subTest(theme=theme):
                rt=self.runtime(team(1),theme)
                for count in (0,1,4,5,9,13,17,21,33,65):
                    st=team(count,master=count//2)
                    rt.ctx.eval(f'scene.initialize({json.dumps(st)},{json.dumps(theme)})')
                    rt.tick(None,AT+100)
                    view=rt.view();g=self.geometry(rt)
                    self.assertEqual(view['capacity'],count)
                    self.assertEqual(len(view['seats']),count)
                    # Compare all permitted column counts, including unused
                    # space in the last row and the complete sleeping area.
                    connected=theme in ('chinese_tech','finance','factory')
                    bedside=theme=='chinese_tech'
                    islands=math.ceil(max(0,count-1)/4)
                    areas=[]
                    for columns in range(1,max(1,islands)+1):
                        width=max(18 if connected else 14,4+columns*(4 if connected else 6))+2
                        office_height=max(8 if bedside else 7,(8 if bedside else 5)+math.ceil(islands/columns)*(10 if bedside else 6))
                        mid=2+office_height
                        height=mid+1 if bedside else mid+math.ceil(count/max(1,(width-4)//2))*3+2
                        areas.append(width*(height+int(bedside)))
                    self.assertEqual(g['width']*g['height'],min(areas),(theme,count,g['width'],g['height']))
                    self.assert_access(g,count)

    def test_middle_retirements_release_holes_and_shrink_without_losing_furniture(self):
        st=team(21)
        rt=self.runtime(st,'chinese_tech')
        before=rt.view()['layout']
        # The final/highest seat survives. Removing only trailing seats would
        # therefore keep the entire old room despite almost everyone leaving.
        st['agents']=[st['agents'][0],st['agents'][20]]
        rt.ctx.eval('scene._debug().tune({refuseShare:0})')
        for i in range(1,2401):
            rt.tick(st if i%3==1 else None,AT+i*100)
            if len(rt.view()['seats'])==2:break
        view=rt.view()
        self.assertEqual(view['capacity'],2)
        self.assertEqual([s['name'] for s in view['seats']],['agent-0','agent-20'])
        self.assertEqual(view['chars']['agent-20']['idx'],1)
        self.assertTrue(all(s['desk'] and s['bed'] for s in view['seats']))
        self.assertLess(view['layout']['width']*view['layout']['height'],before['width']*before['height'])
        self.assert_access(self.geometry(rt),2)
        rt.save()
        restored=self.runtime(st,'chinese_tech',rt.store.get_control('office_scene',''),rt.last_at)
        self.assertEqual(restored.view(),view)

    def test_legacy_oversized_room_preserves_people_speech_and_unfinished_scripts(self):
        st=team(5)
        rt=self.runtime(st,'chinese_tech')
        rt.ctx.eval('''const d=scene._debug();d.layout(33,0,true);
          for(const c of Object.values(d.chars)) {
            const p=d.DESKS[c.idx]?.chair || d.DOOR;
            Object.assign(c,{tx:p[0],ty:p[1],px:p[0]*32,py:p[1]*32,moving:false,steps:[],goal:p});
          }
          const c=d.chars['agent-4'];
          c.setup=[{k:'say',text:'Furniture delivery is still in progress',ms:60000},{k:'goto',where:'bed'}];
          c.carry='bed';c.stepAt=__officeClock;
          d.setSpeech(c,'Preserve the whole conversation during resizing.',__officeClock+60000);''')
        legacy=rt.ctx.execute('scene.checkpoint()')
        legacy.pop('layout')
        legacy['random_state']=1
        legacy['animation_frames']=[{'at':AT,'theme':'chinese_tech','chars':{}}]
        saved=json.dumps(legacy)
        restored=self.runtime(st,'chinese_tech',saved,AT)
        view=restored.view()
        self.assertEqual(view['capacity'],5)
        self.assertEqual(set(view['chars']),set(legacy['chars']))
        self.assertEqual(view['chars']['agent-4']['setup'],legacy['chars']['agent-4']['setup'])
        for field in ('say','sayStart','sayUntil','carry','stepAt','mode'):
            self.assertEqual(view['chars']['agent-4'][field],legacy['chars']['agent-4'][field],field)
        self.assertEqual(len(view['animation_frames']),1)
        self.assertEqual(view['animation_frames'][0]['layout_revision'],view['layout']['revision'])
        self.assert_access(self.geometry(restored),5)
        for name,c in view['chars'].items():
            self.assertGreaterEqual(c['tx'],0,name);self.assertLess(c['tx'],view['layout']['width'],name)
            self.assertGreaterEqual(c['ty'],0,name);self.assertLess(c['ty'],view['layout']['height'],name)
        restored.save()
        again=self.runtime(st,'chinese_tech',restored.store.get_control('office_scene',''),restored.last_at)
        self.assertEqual(again.view(),restored.view())

    def test_adding_one_person_does_not_preallocate_another_four_seats(self):
        st=team(5)
        rt=self.runtime(st,'chinese_tech')
        old=rt.view();g=self.geometry(rt)
        st['agents'].append({**copy.deepcopy(st['agents'][-1]),'name':'new-hire'})
        rt.tick(st,AT+100)
        view=rt.view()
        self.assertEqual(view['capacity'],6)
        self.assertEqual(view['layout'],old['layout'],'the same room already accommodates six people')
        self.assertEqual(self.geometry(rt)['width'],g['width'])
        self.assertEqual(self.geometry(rt)['height'],g['height'])
        self.assertTrue(view['chars']['new-hire']['setup'])
        self.assertFalse(view['seats'][-1]['desk'])
