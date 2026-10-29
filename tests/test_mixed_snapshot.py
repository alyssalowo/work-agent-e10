"""Synthetic mixed capture regressions; no network or business writes."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('mixed_snapshot',Path(__file__).resolve().parents[1]/'.agents/skills/packing-order/scripts/sheet_snapshot.py')
snapshot=importlib.util.module_from_spec(spec);spec.loader.exec_module(snapshot)

def capture():
    def sheet(name,scope):
        full=scope=='full'
        return dict(name=name,sheet_id=name,capture_scope=scope,
            metadata=dict(success=True,id=name,name=name,nonEmptyRange=dict(range='A1:B2',lastColumn='B',lastRow=2,lastCell='B2')),
            response=dict(success=True,hasMore=False,truncationReasons=[],returnedRange='A1:B2' if full else 'B1:B2',
                colIndices=['A','B'] if full else ['B'],rowIndices=[1,2],csv='字段,单号\nv,2301-1\n' if full else '单号\n1902-1\n'))
    return dict(node_id='n',target_url='https://alidocs.dingtalk.com/i/nodes/n',round_id='r',collection_failures=[],recovery_blocked=False,sheets=[sheet('target','full'),sheet('dedup','doc_no_column')])

class MixedSnapshotTests(unittest.TestCase):
    def test_mixed_capture_yields_all_document_numbers(self):
        index=snapshot.document_index(capture())
        self.assertEqual([s['doc_nos'] for s in index['sheets']],[['2301-1'],['1902-1']])
        self.assertEqual(index['scope'],'document_numbers_only')
        self.assertNotIn('complete',index)
    def test_only_full_target_can_become_business_snapshot(self):
        raw=capture();snapshot.document_index(raw)
        self.assertTrue(snapshot.convert({**raw,'sheets':[raw['sheets'][0]]},('target',),'n')['complete'])
        with self.assertRaises(ValueError):snapshot.convert(raw)
        with self.assertRaises(ValueError):snapshot.convert({**raw,'sheets':[raw['sheets'][1]]})
    def test_invalid_or_recovery_capture_never_becomes_dedup_evidence(self):
        for mutate in [lambda x:x.update(recovery_blocked=True),lambda x:x.update(collection_failures=[{'reason':'unknown'}]),
            lambda x:x['sheets'][1]['response'].update(csv='错误\n1902-1\n'),
            lambda x:x['sheets'][1]['response'].update(hasMore=True),
            lambda x:x['sheets'][1]['response'].update(rowIndices=[1,3]),
            lambda x:x['sheets'][1]['response'].update(returnedRange='B1:B1'),
            lambda x:x['sheets'][1].update(capture_scope='recheck_range')]:
            raw=copy.deepcopy(capture());mutate(raw)
            with self.assertRaises(ValueError):snapshot.document_index(raw)

if __name__=='__main__':unittest.main()
