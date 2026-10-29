"""Offline regressions: synthetic data only; no ERP, credentials or network."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / '.agents/skills'

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

plan = load('shipment_plan', SKILLS / 'e10-shipment-sheet-fill/scripts/plan.py')
review = load('local_review', SKILLS / 'packing-order/scripts/local_review.py')
append = load('shipment_append_plan', SKILLS / 'packing-order/scripts/shipment_append_plan.py')
routing = load('workorder_sources', SKILLS / 'packing-order/scripts/workorder_sources.py')
evidence_fixture = load('evidence_fixture', ROOT / 'tests/test_source_evidence.py')

def row(module='sales', **changes):
    value = dict(module=module, doc_no={'sales':'2301-001','borrow':'1902-001',
                 'outbound_application':'1018-001'}[module], detail_id='D1',
                 doc_date='2026-10-29', expected_date='2026-10-31',
                 doc_type_name='真实类型', doc_type_code='1902' if module=='borrow' else '2301',
                 item_code='1-V001', item_name='正常成品', columns={})
    if module=='sales': value.update(outbound_status='未出库', delivery_label='S1',approval_status='A')
    if module=='borrow': value['borrow_detail_ended']=False
    value.update(changes)
    return value

def sheet(day, **changes):
    value=dict(sheet_id=day, name=day, date=day, doc_nos=[],
               headers=['单号','销货明细_品号','单据类型','预计发货日期','业务员','备注','交付标签'])
    value.update(changes)
    return value

def payload(rows, days=('2026-10-29','2026-10-30','2026-10-31')):
    return dict(records_complete=True, sheets_complete=True,sales_approval_options={'A':'已审核','X':'作废'},
                modules_complete=sorted(plan.MODULES), records=rows, sheets=[sheet(d) for d in days])

def build(value): return plan.build_plan(value, '2026-10-29')

class ShipmentTests(unittest.TestCase):
    def test_three_sources_real_fields(self):
        result=build(payload([row(),row('borrow'),row('outbound_application')]))
        self.assertTrue(result['all_modules_complete'])
        self.assertEqual(result['counts']['planned_rows'],3)
        self.assertEqual([s['date'] for s in result['sheets']],['2026-10-30','2026-10-31'])

    def test_borrow_ended_is_detail_level(self):
        result=build(payload([row('borrow'),row('borrow',detail_id='D2',borrow_detail_ended=True)]))
        self.assertEqual(result['counts']['planned_rows'],1)
        self.assertEqual(result['excluded_details'][0]['reason'],'borrow_detail_ended')
        self.assertEqual(result['sheets'][0]['values'][0][2],'送样借出单')

    def test_borrow_bool_required(self):
        for value in (None, 'false', 0, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build(payload([row('borrow',borrow_detail_ended=value)]))

    def test_borrow_no_sales_status_or_advance(self):
        r=row('borrow',outbound_status='出库结束')
        result=build(payload([r]))
        self.assertEqual(result['sheets'][0]['date'],'2026-10-31')

    def test_application_mapping_and_alias_headers(self):
        r=row('outbound_application',columns={'可发货日期':'2026-10-31','申请人员':'申请人',
              '出库申请明细_备注':'明细','备注':'单头'})
        data=payload([r]);data['sheets'][-1]['headers'][1]='借出明细_品号'
        values=build(data)['sheets'][0]['values'][0]
        self.assertEqual(values[1],'1-V001');self.assertEqual(values[4],'申请人')
        self.assertEqual(values[5],'明细/单头');self.assertEqual(values[6],'')

    def test_application_prefixes(self):
        rows=[row('outbound_application',doc_no=p+'-1',detail_id=p) for p in ('1018','1008','1012','9999')]
        result=build(payload(rows))
        self.assertEqual(result['counts']['planned_rows'],3)
        self.assertEqual(result['excluded_documents'][0]['reason'],'outbound_application_prefix')

    def test_application_no_foreign_status_filters(self):
        result=build(payload([row('outbound_application',outbound_status='出库结束',borrow_detail_ended=True)]))
        self.assertEqual(result['counts']['planned_rows'],1)

    def test_filtered_detail_still_controls_sales_label(self):
        result=build(payload([row(),row(detail_id='D2',item_code='E001',delivery_label='')]))
        self.assertEqual(result['sheets'][0]['date'],'2026-10-31')
        self.assertEqual(result['counts']['detail_filtered_rows'],1)

    def test_only_sales_can_fallback(self):
        result=build(payload([row(),row('borrow'),row('outbound_application')],days=('2026-10-29',)))
        self.assertEqual(result['sheets'][0]['date'],'2026-10-29')
        self.assertEqual(result['counts']['missing_rows'],2)

    def test_today_tomorrow_no_advance(self):
        for day in ('2026-10-29','2026-10-30'):
            self.assertEqual(build(payload([row(expected_date=day)]))['sheets'][0]['date'],day)

    def test_nondated_sheet_dedup_full_number(self):
        data=payload([row(),row(doc_no='2301-0010',detail_id='D2')])
        data['sheets'].append(sheet(None,sheet_id='archive',name='归档',doc_nos=[' 2301-001 ']))
        result=build(data)
        self.assertEqual(result['counts']['existing_rows'],1)
        self.assertEqual(result['counts']['planned_rows'],1)

    def test_all_filtered_zero_is_explicit(self):
        result=build(payload([row(item_code='E001')]))
        self.assertEqual(result['sheets'],[]);self.assertTrue(result['all_modules_complete'])

    def test_module_scope_and_completeness(self):
        data=payload([]);data['modules_complete']=['sales']
        self.assertFalse(build(data)['all_modules_complete'])
        for key in ('modules_complete','records_complete','sheets_complete'):
            bad=payload([]);bad.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):build(bad)

    def test_mapping_conflict_fails(self):
        for columns in ({'申请人员':'甲','业务员':'乙'},{'可发货日期':'2026-11-01'},
                        {'销货明细_品号':'A','出库申请明细_品号':'B'}):
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                build(payload([row('outbound_application',columns=columns)]))

    def test_duplicate_identity_head_or_sheet_fails(self):
        cases=[payload([row(),row()]),payload([row(),row(detail_id='D2',expected_date='2026-11-01')]),
               payload([row(),row('borrow',doc_no='2301-001')])]
        duplicate=payload([]);duplicate['sheets'].append(copy.deepcopy(duplicate['sheets'][0]));cases.append(duplicate)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):build(data)

    def test_invalid_sales_status_date_label_and_nan(self):
        for r in (row(outbound_status='未知'),row(expected_date='2026-02-30'),
                  row('borrow',delivery_label='S1'),row(columns={'数值':float('nan')})):
            with self.subTest(row=r), self.assertRaises(ValueError):build(payload([r]))

    def test_exact_stage_specific_exclusions(self):
        rows=[row(detail_id=str(i),item_code=sku,item_name=name) for i,(sku,name) in enumerate(
              [('1-V001','S263'),('1-V002','S630'),('1-V003','L630'),('1-H001','DSN'),('1-V004','s263')])]
        result=build(payload(rows))
        self.assertEqual(result['counts']['detail_filtered_rows'],3)
        self.assertEqual(result['counts']['planned_rows'],2)

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def write(self,name,value):
        path=self.root/name;path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        return {'path':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    def handoff(self,packet,checks=None):
        ref=self.write('receipt.json',packet);ref.update(role='receipt',pointers=['/count'])
        manifest={'schema_version':1,'task_id':'synthetic','stage':'test','authorization':'synthetic only',
                  'exclusions':[],'next_step':'main review','inputs':[ref],
                  'checks':checks if checks is not None else [
                    {'name':'success','role':'receipt','pointer':'/ok','expected':True},
                    {'name':'zero count','role':'receipt','pointer':'/count','expected':0}]}
        self.write('manifest.json',manifest)
        return review.handoff(self.root/'manifest.json',self.root/'out')
    def test_handoff_zero_real_values_and_hashes(self):
        result=self.handoff({'ok':True,'count':0})
        self.assertEqual(result['check_state'],'checks_passed')
        out=json.loads((self.root/'out/handoff-checks.json').read_text(encoding='utf-8'))
        self.assertEqual(out['business_gate'],'not_evaluated')
        self.assertEqual(out['evidence'][0]['facts'][0]['value'],0)
    def test_handoff_false_blocks(self):
        self.assertEqual(self.handoff({'ok':False,'count':0})['check_state'],'blocked')
    def test_handoff_missing_field_blocks(self):
        self.assertEqual(self.handoff({'count':0})['blocked_checks'],['success'])
    def test_handoff_bool_not_number(self):
        self.assertEqual(self.handoff({'ok':1,'count':0})['check_state'],'blocked')
    def test_handoff_does_not_overwrite(self):
        self.handoff({'ok':True,'count':0})
        with self.assertRaises(ValueError):review.handoff(self.root/'manifest.json',self.root/'out')
    def test_hash_tampering_rejected(self):
        self.handoff({'ok':True,'count':0})
        self.write('receipt.json',{'ok':True,'count':1})
        with self.assertRaises(ValueError):review.handoff(self.root/'manifest.json',self.root/'different')
    def test_pointer_escaping(self):
        self.assertEqual(review.pointer({'a/b':{'~':0}},'/a~1b/~0'),0)
        with self.assertRaises(ValueError):review.pointer({},'/bad~2')
    def test_long_evidence_is_indexed_without_copying(self):
        original='明细\n'*1000
        ref=self.write('large.json',{'rows_text':original});ref.update(role='source',pointers=['/rows_text'])
        self.write('index-manifest.json',{'schema_version':1,'task_id':'test','purpose':'final_report','inputs':[ref]})
        review.evidence(self.root/'index-manifest.json',self.root/'index')
        index=json.loads((self.root/'index/evidence-index.json').read_text(encoding='utf-8'))
        fact=index['files'][0]['locations'][0]
        self.assertNotIn('value',fact)
        self.assertEqual(fact['characters'],len(original))
        self.assertEqual(fact['sha256'],hashlib.sha256(original.encode('utf-8')).hexdigest())
    def workorders(self,mutator=lambda p:None):
        filters={'doc_no_prefix':'5101-','date_from':'2026-01-01',
                 'excluded_approval_status':'V','excluded_statuses':['y','Y']}
        packet={'ok':True,'filters':filters,'start_row':1,'next_start_row':2,'has_next_page':False,
                'rows':[{'ID':'id','DOC_NO':'5101-1','DOC_DATE':'2026-10-29','APPROVAL_STATUS':'Y',
                         'STATUS':'N','DEMAND_NO':'d','ITEM_CODE':'p','ITEM_FEATURE_CODE':'','PLAN_QTY':'1.25'}]}
        mutator(packet)
        ref=self.write('page.json',packet)
        self.write('pages.json',{'schema_version':1,'task_id':'test','filters':filters,'pages':[ref]})
        return review.workorders(self.root/'pages.json',self.root/'source-out')
    def test_workorder_source_good(self):
        self.assertEqual(self.workorders()['rows'],1)
    def test_workorder_pagination_unknown_rejected(self):
        with self.assertRaises(ValueError):self.workorders(lambda p:p.update(has_next_page=None))
    def test_workorder_Y_and_y_excluded(self):
        for status in ('Y','y'):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.workorders(lambda p:p['rows'][0].update(STATUS=status))

class AppendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def save(self,name,value):
        path=self.root/name;path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    def test_shared_manual_source_prefixes(self):
        for no in ('1902-001','1018-001','1008-001','1012-001'):
            self.assertTrue(routing.is_manual_source(no),no)
        for no in ('2301-001','01018-001','',None):
            self.assertFalse(routing.is_manual_source(no),no)
    def fixture(self,header):
        headers=['预计发货日期',header]
        options={'A':'已审核','X':'作废'}
        record=row(head_id='h',detail_id='d',delivery_label='',columns={'销货明细_业务数量':'1.25'})
        data=payload([record],days=('2026-10-31',));data['sheets'][0].update(name='s',sheet_id='id',headers=headers)
        data.update(workflow_kind='shipment_fill',scope=['sales'],modules_complete=['sales'],sales_approval_options=options)
        evidence_fixture.add_evidence(data,self.save)
        ref=self.save('input.json',data);source=plan.build_plan(data,'2026-10-29')
        source.update(node_id='n',allowed_write_sheets=['s'],source_binding={'input_path':ref['path'],'input_sha256':ref['sha256']})
        snapshot={'schema_version':1,'ok':True,'complete':True,'transport':'dingtalk_sheet','node_id':'n',
                  'target_url':'https://alidocs.dingtalk.com/i/nodes/n',
                  'sheets':[{'name':'s','sheet_id':'id','scope_complete':True,'range':'A1:B1','tsv':'\t'.join(headers)+'\n'}]}
        return source,snapshot
    def test_both_quantity_headers_supported(self):
        for header in ('销货明细_业务数量','借出明细_业务数量'):
            src,snap=self.fixture(header)
            self.assertIn(header,append.build(src,snap,['s'],'shipment_fill',['sales'])['requests'][0]['type_columns'])
    def test_identity_and_authorization_preserved(self):
        src,snap=self.fixture('销货明细_业务数量')
        with self.assertRaises(ValueError):append.build(src,snap,['other'],'shipment_fill',['sales'])
        snap['node_id']='other'
        with self.assertRaises(ValueError):append.build(src,snap,['s'],'shipment_fill',['sales'])
    def bound_plan(self):
        src,snap=self.fixture('销货明细_业务数量')
        source_ref=self.save('source.json',src);snapshot_ref=self.save('snapshot.json',snap)
        result=append.build(src,snap,['s'],'shipment_fill',['sales'])
        result.update(source_plan_path=source_ref['path'],source_plan_sha256=source_ref['sha256'],snapshot_path=snapshot_ref['path'],snapshot_sha256=snapshot_ref['sha256'])
        return Path(self.save('append.json',result)['path'])
    def test_fresh_append_verifier_rebuilds_and_rejects_altered_plan(self):
        path=self.bound_plan()
        self.assertEqual(append.verify(path),{'ok':True,'plan_sha256':append.digest(path)})
        obj=json.loads(path.read_text(encoding='utf8'));obj['requests'][0]['values'][0][1]='8'
        path.write_text(json.dumps(obj),encoding='utf8')
        with self.assertRaises(ValueError):append.verify(path)
    def test_fresh_append_verifier_rechecks_original_pages(self):
        path=self.bound_plan()
        page=self.root/'sales.json';page.write_text('{}',encoding='utf8')
        with self.assertRaises(ValueError):append.verify(path)

if __name__=='__main__':unittest.main()
