"""Cache billing, persisted provenance and safe legacy repairs across model switches."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from huntun.config import agents_dir
from huntun.memory import AgentMemory
from huntun.models import cost_usd, estimate_cost_usd
from huntun.performance import record_usage
from huntun.server import _totals
from huntun.store import Store
from huntun.types import AgentState
from huntun.usage import add_usage, cost_summary, journal_usage, migrate_legacy_usage, token_total


class UsageTests(unittest.TestCase):
    def test_model_specific_cache_prices_and_mixed_ttls(self):
        self.assertEqual(cost_usd('claude-opus-5-5', 0, 0, 1_000_000), .20)
        self.assertEqual(cost_usd('claude-sonnet-5', 0, 0, 1_000_000), .20)
        self.assertEqual(cost_usd('claude-opus-5-5', 1_000_000, 1_000_000, 1_000_000,
                                  1_000_000, cache_write_1h=500_000), 30.70)
        self.assertEqual(cost_usd('deepseek-v4-pro', 0, 0, 1_000_000), .044)
        self.assertEqual(cost_usd('deepseek-flash', 0, 0, 1_000_000), .006)
        self.assertEqual(cost_usd('deepseek-flash', 0, 0, 0, 1_000_000), .30)
        self.assertIsNone(cost_usd('unknown-provider/model', 100, 50))
        self.assertEqual(estimate_cost_usd('claude-opus-5-5', 1_000_000), 4.79)

    def test_cost_provenance_survives_reload_and_model_switch(self):
        state = AgentState()
        add_usage(state, {'input':10, 'cache_read':90, 'cache_write':5, 'output':20, 'cost_usd':.002}, 'api', 'estimated')
        self.assertEqual(token_total(state.usage_totals), 125)
        self.assertTrue(cost_summary(state)['complete'])
        add_usage(state, {'input':40, 'cache_read':60, 'output':10, 'cost_usd':0}, 'codex')
        self.assertFalse(cost_summary(state)['complete'])
        self.assertEqual(cost_summary(state)['kinds'], ['estimated', 'untracked'])
        with tempfile.TemporaryDirectory() as directory:
            memory = AgentMemory(Path(directory), 'dev')
            memory.state = state
            memory.save_state()
            self.assertEqual(cost_summary(AgentMemory(Path(directory), 'dev').state), cost_summary(state))
        old = AgentState(usage_totals={'input':99, 'cost_usd':42})
        add_usage(old, {'input':1, 'cost_usd':0}, 'llamacpp', 'local')
        self.assertEqual(old.usage_totals['cost_usd'], 42)
        self.assertIn('legacy', cost_summary(old)['kinds'])
        self.assertFalse(cost_summary(old)['complete'])

    def test_header_includes_cache_writes_and_unknown_costs(self):
        state = AgentState()
        add_usage(state, {'input':10,'output':20,'cache_read':30,'cache_write':40,'cost_usd':0}, 'codex')
        orch = SimpleNamespace(team=[SimpleNamespace(name='dev', backend='')], backend_name='codex',
                               runtimes={'dev':SimpleNamespace(memory=SimpleNamespace(state=state))},
                               backend_name_for=lambda _: 'codex')
        totals = _totals(orch, Path('/unused'))
        self.assertEqual(totals['tokens'], 100)
        self.assertFalse(totals['usage_cost']['complete'])
        self.assertEqual(totals['usage_cost']['kinds'], ['untracked'])

    def test_legacy_repair_is_evidence_based_idempotent_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = Store(workspace / '.huntun' / 'board.sqlite')
            self.addCleanup(store.close)
            memory = AgentMemory(agents_dir(workspace), 'dev')
            memory.state.usage_totals = {'input':300, 'cache_read':110, 'output':20, 'cost_usd':9}
            memory.save_state()
            unknown = {'usage':{'input':200,'cache_read':50,'output':10,'cost_usd':9}}
            known = {'backend':'codex','model':'gpt-example','usage':{'input':100,'cache_read':60,'output':10}}
            key = record_usage(store, 'dev', known['usage'], accounting_version=1)
            memory.journal({**known, 'usage_sample':key})
            memory.journal(unknown)  # may be a different backend: keep it verbatim
            original = (memory.dir / 'journal.jsonl').read_bytes()
            migrate_legacy_usage(workspace, store)
            repaired = AgentMemory(agents_dir(workspace), 'dev').state
            self.assertEqual(repaired.usage_totals['input'], 240)
            self.assertEqual(repaired.usage_totals['cost_usd'], 9)
            self.assertEqual(store._one('SELECT input FROM performance_usage WHERE key=?',key)['input'],40)
            self.assertEqual((memory.dir / 'journal.jsonl').read_bytes(), original)
            self.assertEqual(json.loads((memory.dir / 'usage-v1-state.json').read_text())['usage_totals']['input'],300)
            migrate_legacy_usage(workspace, store)
            self.assertEqual(AgentMemory(agents_dir(workspace), 'dev').state.usage_totals['input'],240)
            self.assertEqual(journal_usage(unknown), unknown['usage'])
            normalized = {**known,'usage_accounting_version':2,'usage':{'input':40,'cache_read':60,'output':10}}
            self.assertEqual(journal_usage(normalized)['input'],40)
