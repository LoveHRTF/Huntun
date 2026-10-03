"""Historical imports, correct time buckets, commit attribution and project isolation."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from huntun.config import agents_dir
from huntun.gitops import ensure_repo, ensure_worktree, git, sync_worktree
from huntun.performance import record_commit, record_cycle_commits, record_usage, review
from huntun.store import Store
from huntun.types import AgentSpec

NOW = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
AT = '2026-10-02T11:05:00Z'


class PerformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.store = Store(self.project / '.huntun' / 'board.sqlite')
        self.addCleanup(self.store.close)
        self.team = [AgentSpec('master', 'master', 'Master', 'Lead'), AgentSpec('dev', 'backend', 'Developer', 'Build')]

    def view(self, period='24h', bin_size='auto'):
        return review(self.store, self.project, self.team, period, bin_size=bin_size, now=NOW)

    def journal(self, name, entries):
        file = agents_dir(self.project) / name / 'journal.jsonl'
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open('a') as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + '\n')
        return file

    async def test_time_buckets_full_history_cache_and_duplicate_commits(self):
        self.journal('dev', [{'at':AT, 'usage':{'input':100,'output':20,'cache_read':30,'cache_write':5}, 'commits':['abcdef0123456']}])
        self.store.log_event('dev', 'commit', 'abcdef0 Build API')
        self.store._exec("UPDATE events SET created_at=?", AT)
        thread = self.store.create_thread('dev', 'API', '@master Ready')
        self.store.add_comment(thread['id'], 'dev', '@master Tested')
        self.store.add_comment(thread['id'], 'human', '@dev Good job')
        for table in ('threads', 'comments', 'mentions'):
            self.store._exec(f"UPDATE {table} SET created_at=?", AT)
        a = next(a for a in self.view()['agents'] if a['name'] == 'dev')
        self.assertEqual(a['totals'], {'tokens':155,'messages':2,'commits':1,'threads':1,'replies':1,'received':1})
        self.assertEqual(a['tokens'][23],155)
        self.assertEqual(a['messages'][23],2)
        self.assertEqual(a['commits'][23],1)
        self.assertEqual(self.view(),self.view(), 'repeated polls do not reimport or duplicate')
        self.journal('dev', [{'at':'2026-10-02T12:00:00+00:00','usage':{'output':45},'commits':['abcdef0123456','0123456789abc']}])
        a = next(a for a in self.view()['agents'] if a['name']=='dev')
        self.assertEqual(a['totals']['tokens'],200)
        self.assertEqual(a['totals']['commits'],2)
        self.assertEqual(a['tokens'][24],45)
        self.assertEqual(self.store._one('SELECT COUNT(*) AS n FROM performance_usage')['n'],2)

    async def test_new_usage_including_paused_cycles_not_double_counted_by_journal(self):
        key = record_usage(self.store,'dev',{'input':120,'output':10},at=AT)
        record_usage(self.store,'dev',{'input':30},at=AT)  # paused/error result also recorded
        self.journal('dev',[{'at':AT,'usage':{'input':120,'output':10},'usage_sample':key}])
        a = next(a for a in self.view()['agents'] if a['name']=='dev')
        self.assertEqual(a['totals']['tokens'],160)
        reopened = Store(self.project / '.huntun' / 'board.sqlite')
        self.addCleanup(reopened.close)
        self.assertEqual(review(reopened,self.project,self.team,'24h',now=NOW),self.view())

    async def test_partial_append_old_ranges_retired_agents_and_project_isolation(self):
        f = self.journal('retired-dev',[{'at':'2024-01-01T00:00:00Z','usage':{'input':17},'commits':[]}])
        with f.open('a') as fh:
            fh.write('{"at":')
        self.assertTrue(self.view()['history_import_pending'])
        self.assertEqual(next(a for a in self.view()['agents'] if a['name']=='retired-dev')['totals']['tokens'],0)
        all_view = self.view('all')
        self.assertLessEqual(len(all_view['buckets']),62)
        retired = next(a for a in all_view['agents'] if a['name']=='retired-dev')
        self.assertTrue(retired['retired'])
        self.assertEqual(retired['totals']['tokens'],17)
        with f.open('a') as fh:
            fh.write('"2026-10-02T11:05:00Z","usage":{"input":3}}\n')
        self.assertFalse(self.view()['history_import_pending'])
        self.assertEqual(next(a for a in self.view()['agents'] if a['name']=='retired-dev')['totals']['tokens'],3)
        other = Store(Path(self.tmp.name) / 'other' / '.huntun' / 'board.sqlite')
        self.addCleanup(other.close)
        self.assertTrue(all(not a['totals']['tokens'] for a in review(other,Path(self.tmp.name)/'other',self.team,'all',now=NOW)['agents']))
        with self.assertRaises(ValueError):
            self.view('bad')

    async def test_custom_bins_keep_the_exact_window_totals_and_partial_coverage(self):
        record_usage(self.store,'dev',{'input':3},at='2026-10-01T12:30:00Z')  # before the rolling window
        record_usage(self.store,'dev',{'input':7},at='2026-10-01T12:30:01Z')
        record_usage(self.store,'dev',{'input':11},at='2026-10-02T12:30:00Z')  # current observed second
        record_usage(self.store,'dev',{'input':100},at='2026-10-02T12:30:01Z')  # future, never counted
        windows=[]
        for interval in ('5m','15m','1h','6h','1d'):
            data=self.view(bin_size=interval)
            agent=next(a for a in data['agents'] if a['name']=='dev')
            self.assertEqual(agent['totals']['tokens'],18)
            self.assertEqual(sum(data['bucket_coverage_seconds']),86400)
            self.assertLessEqual(len(data['buckets']),512)
            windows.append((data['window_start'],data['window_end']))
            self.assertTrue(any(data['partial_buckets']))
            self.assertEqual(data['bin'],interval)
        self.assertEqual(len(set(windows)),1,'a bin change must never change the date window')
        with self.assertRaisesRegex(ValueError,'maximum is 512'):
            self.view('30d','1m')
        with self.assertRaisesRegex(ValueError,'bin must be'):
            self.view(bin_size='bogus')
        fine=self.view('1h','1m')
        self.assertEqual(len(fine['buckets']),61)
        self.assertEqual(sum(fine['bucket_coverage_seconds']),3600)
        options=self.view('7d')['bin_options']
        self.assertFalse(next(o for o in options if o['id']=='15m')['available'])
        self.assertTrue(next(o for o in options if o['id']=='1h')['available'])

    async def test_weekly_bins_start_on_monday_and_do_not_include_future_posts(self):
        self.journal('dev',[{'at':'2026-09-12T14:00:00Z','usage':{'input':9}}])
        data=self.view('30d','1w')
        self.assertTrue(all(datetime.fromisoformat(at).weekday()==0 for at in data['buckets']))
        self.assertEqual(sum(data['bucket_coverage_seconds']),30*86400)

    async def test_watchdog_row_counts_real_dialog_replies_and_usage(self):
        self.store.watchdog_message('assistant','Recovered master','master','test-model','high')
        self.store.watchdog_message('user','Wake master','master','test-model','high')
        self.store._exec("UPDATE watchdog_messages SET created_at=?",AT)
        record_usage(self.store,'local-watchdog',{'input':9,'output':2},at=AT)
        view=self.view('all')
        agent=next(a for a in view['agents'] if a['name']=='local-watchdog')
        self.assertFalse(agent['retired'])
        self.assertEqual(agent['role'],'watchdog')
        self.assertEqual(agent['totals']['tokens'],11)
        self.assertEqual(agent['totals']['messages'],1)
        self.assertEqual(agent['totals']['replies'],1)

    async def test_existing_native_commit_is_attributed_from_its_worktree_reflog(self):
        await ensure_repo(self.project)
        tree = await ensure_worktree(self.project,'dev')
        (tree/'legacy.txt').write_text('Existing Codex work')
        await git(tree,'add','legacy.txt')
        await git(tree,'-c','user.name=Local Person','-c','user.email=person@example.test','commit','-m','Older native work',
                  env={'GIT_AUTHOR_DATE':AT,'GIT_COMMITTER_DATE':AT})
        first=self.view('all')
        a=next(a for a in first['agents'] if a['name']=='dev')
        self.assertEqual(a['totals']['commits'],1)
        self.assertEqual(self.view('all'),first)

    async def test_native_commits_are_counted_once_and_exclude_merged_peer_work(self):
        await ensure_repo(self.project)
        tree = await ensure_worktree(self.project,'dev')
        baseline = await git(tree,'rev-parse','HEAD')
        (tree/'native.txt').write_text('one')
        await git(tree,'add','native.txt')
        await git(tree,'-c','user.name=Local Person','-c','user.email=person@example.test','commit','-m','Native CLI work')
        sha = await git(tree,'rev-parse','HEAD')
        record_commit(self.store,'dev',sha[:7],AT)
        await record_cycle_commits(self.store,tree,'dev',baseline)
        peer = await ensure_worktree(self.project,'qa')
        (peer/'peer.txt').write_text('peer')
        await git(peer,'add','peer.txt')
        await git(peer,'-c','user.name=qa','-c','user.email=qa@huntun.local','commit','-m','Peer work')
        await git(self.project,'merge','--ff-only','huntun/agents/qa')
        await sync_worktree(self.project,tree,'dev')
        await record_cycle_commits(self.store,tree,'dev',baseline)
        commits = self.store._q("SELECT * FROM performance_commits WHERE agent='dev'")
        self.assertEqual(len(commits),1)
        self.assertEqual(commits[0]['sha'],sha)
