import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path); obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj
plan=load('shipment_plan_evidence',ROOT/'.agents/skills/e10-shipment-sheet-fill/scripts/plan.py')
ev=load('source_evidence_test',ROOT/'.agents/skills/packing-order/scripts/source_evidence.py')
append=load('append_source_evidence_test',ROOT/'.agents/skills/packing-order/scripts/shipment_append_plan.py')

def add_evidence(data, save):
    data['field_bindings']={};data['source_evidence']={}
    for field, optname, refname, module, options in [
        ('APPROVAL_STATUS','sales_approval_options','sales_approval_metadata','SALES_DELIVERY',{'A':'已审核','X':'作废'}),
        ('ISSUED_STATUS','sales_issued_options','sales_issued_metadata','SALES_DELIVERY',{'I':'未出库','P':'部分出库'}),
        ('CLOSE','borrow_close_options','borrow_close_metadata','BORROW_DOC',{'N':'未结束','Y':'已结束'})]:
        data[optname]=options
        ref=save(refname+'.json',{'fields':{'state':{'module':module,'field':field,'options':options}}})
        ref['pointer']='/fields/state';data[refname]=ref
    for module in data['scope']:
        remote=ev.MODULES[module]
        mapping={'fields':{'doc_type_name':'DOC_NAME','doc_type_code':'DOC_CODE','item_code':'ITEM_CODE','item_name':'ITEM_DESCRIPTION'},'columns':{}}
        if module=='sales':mapping['fields'].update(delivery_label='DELIVERY_LABEL',outbound_status='ISSUED_STATUS')
        if module=='borrow':mapping['fields']['borrow_detail_ended']='CLOSE'
        rows=[r for r in data['records'] if r['module']==module]
        for r in rows:
            for key in r['columns']:mapping['columns'][key]='COL_'+str(len(mapping['columns'])) if key not in mapping['columns'] else mapping['columns'][key]
        data['field_bindings'][module]=mapping
        aliases={'HEAD_ID':remote+'_ID','DETAIL_ID':remote+'_D.'+remote+'_D_ID','DOC_NO':'DOC_NO','DOC_DATE':'DOC_DATE','EXPECTED_DATE':'EXPECTED'}
        for alias in list(mapping['fields'].values())+list(mapping['columns'].values()): aliases[alias]=('BORROW_DOC_D.CLOSE' if alias=='CLOSE' else alias)
        if module=='sales':aliases['APPROVAL_STATUS']='APPROVAL_STATUS'
        filters=dict(doc_date_from='2026-01-01',expected_from='2026-10-29',expected_path='EXPECTED',status_path='ISSUED_STATUS' if module=='sales' else None,status_end='3' if module=='sales' else None,borrow_category='19' if module=='borrow' else None,stock_requisition_prefixes=['1018','1008','1012'] if module=='outbound_application' else None)
        raw=[]
        for r in rows:
            item=dict(HEAD_ID=r['head_id'],DETAIL_ID=r['detail_id'],DOC_NO=r['doc_no'],DOC_DATE=r['doc_date'],EXPECTED_DATE=r['expected_date'])
            for k,a in mapping['fields'].items():
                value=r[k]
                if k=='outbound_status':value=next(key for key,v in data['sales_issued_options'].items() if v==value)
                if k=='borrow_detail_ended':value='Y' if value else 'N'
                item[a]=value
            for k,a in mapping['columns'].items():item[a]=r['columns'][k]
            if module=='sales':item['APPROVAL_STATUS']=r['approval_status']
            raw.append(item)
        args=dict(module=remote,start_row=1,expected_from=filters['expected_from'],expected_path='EXPECTED',status_path=filters['status_path'],status_end=filters['status_end'],fields=[{'alias':a,'path':path} for a,path in aliases.items()])
        ref=save(module+'.json',dict(ok=True,read_only=True,module=remote,filters=filters,start_row=1,next_start_row=1+len(raw),has_next_page=False,rows=raw))
        ref['request']=save(module+'-request.json',{'name':'e10_query_shipment_details','arguments':args})
        data['source_evidence'][module]=[ref]

class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.dir=Path(self.temp.name)
        self.rows=[dict(module='sales',head_id='H1',detail_id='D1',doc_no='2301-1',doc_date='2026-10-29',expected_date='2026-10-31',doc_type_name='类型',doc_type_code='2301',item_code='A',item_name='正常',columns={'销货明细_业务数量':'1.25'},outbound_status='未出库',delivery_label='',approval_status='A')]
        self.data=dict(workflow_kind='packing_order',scope=list(ev.MODULES),records_complete=True,sheets_complete=True,modules_complete=list(ev.MODULES),records=self.rows,sheets=[dict(sheet_id='s',name='s',date='2026-10-31',headers=['单号'],doc_nos=[])],source_evidence={})
        add_evidence(self.data,self.save)
    def tearDown(self):self.temp.cleanup()
    def save(self,name,obj):
        p=self.dir/name;p.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf-8');return dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    def source(self):
        ref=self.save('input.json',self.data); result=plan.build_plan(self.data,'2026-10-29');result['source_binding']={'input_path':ref['path'],'input_sha256':ref['sha256']};return result
    def test_three_sources_and_zero_pages(self):self.assertEqual(ev.validate(self.source())['workflow_kind'],'packing_order')
    def test_missing_kind_scope_or_source(self):
        for change in ('kind','scope','source'):
            data=copy.deepcopy(self.data)
            if change=='kind': data.pop('workflow_kind')
            elif change=='scope':data.pop('scope')
            else:data['source_evidence'].pop('borrow')
            self.data=data
            with self.assertRaises(ValueError):ev.validate(self.source())
            self.tearDown(); self.setUp()
    def test_full_cannot_limit_scope(self):
        self.data['scope']=['sales'];self.data['modules_complete']=['sales'];self.data['source_evidence']={'sales':self.data['source_evidence']['sales']}
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_single_module_explicit(self):
        self.data.update(workflow_kind='shipment_fill',scope=['sales'],modules_complete=['sales'],source_evidence={'sales':self.data['source_evidence']['sales']},field_bindings={'sales':self.data['field_bindings']['sales']})
        self.assertEqual(ev.validate(self.source())['scope'],['sales'])
    def test_missing_page_and_fake_booleans(self):
        p=self.dir/'sales.json';obj=json.loads(p.read_text(encoding='utf-8'));obj['has_next_page']=True
        self.data['source_evidence']['sales'][0].update(self.save('sales.json',obj))
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_wrong_hash(self):
        self.data['source_evidence']['sales'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_changed_input_and_planner_output(self):
        source=self.source();source['counts']['planned_rows']=0
        with self.assertRaises(ValueError):ev.validate(source)
        source=self.source();Path(source['source_binding']['input_path']).write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):ev.validate(source)
    def test_append_rejects_self_declared_completeness(self):
        with self.assertRaises(ValueError):
            append.build({'status':'prepared_only','all_modules_complete':True}, {}, ['s'])
    def test_two_page_chain(self):
        p=self.dir/'sales.json';first=json.loads(p.read_text(encoding='utf-8'))
        first['has_next_page']=True
        second=copy.deepcopy(first);second.update(start_row=2,next_start_row=2,has_next_page=False,rows=[])
        req=self.data['source_evidence']['sales'][0]['request']; args=json.loads(Path(req['path']).read_text(encoding='utf8')); args['arguments']['start_row']=2
        one=self.save('first.json',first);one['request']=req
        two=self.save('second.json',second);two['request']=self.save('second-request.json',args)
        self.data['source_evidence']['sales']=[one,two]
        self.assertEqual(ev.validate(self.source())['workflow_kind'],'packing_order')
        second['start_row']=3;self.data['source_evidence']['sales'][1].update(self.save('second.json',second))
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_omitted_normalized_row(self):
        self.data['records']=[]
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_page_failure_or_unpermitted(self):
        p=self.dir/'sales.json';obj=json.loads(p.read_text(encoding='utf-8'));obj.update(ok=False,read_only=False)
        self.data['source_evidence']['sales'][0].update(self.save('sales.json',obj))
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_changed_raw_quantity_label_and_issued_status(self):
        for field,value in [('COL_0','9.99'),('DELIVERY_LABEL','S9'),('ISSUED_STATUS','P')]:
            with self.subTest(field=field):
                self.tearDown();self.setUp()
                page=json.loads((self.dir/'sales.json').read_text(encoding='utf8'))
                page['rows'][0][field]=value
                self.data['source_evidence']['sales'][0].update(self.save('sales.json',page))
                with self.assertRaises(ValueError):ev.validate(self.source())
    def test_borrow_close_and_missing_fields(self):
        self.data['records'].append(dict(module='borrow',head_id='BH',detail_id='BD',doc_no='1902-1',doc_date='2026-10-29',expected_date='2026-10-31',doc_type_name='类型',doc_type_code='1902',item_code='A',item_name='正常',columns={},borrow_detail_ended=False))
        add_evidence(self.data,self.save)
        page=json.loads((self.dir/'borrow.json').read_text(encoding='utf8'))
        page['rows'][0]['CLOSE']='Y'
        self.data['source_evidence']['borrow'][0].update(self.save('borrow.json',page))
        with self.assertRaises(ValueError):ev.validate(self.source())
        page['rows'][0].pop('CLOSE')
        self.data['source_evidence']['borrow'][0].update(self.save('borrow.json',page))
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_forged_options_and_request_field(self):
        self.data['sales_issued_options']['I']='部分出库'
        with self.assertRaises(ValueError):ev.validate(self.source())
        self.data['sales_issued_options']['I']='未出库'
        req=self.data['source_evidence']['sales'][0]['request']
        obj=json.loads(Path(req['path']).read_text(encoding='utf8'))
        obj['arguments']['fields']=[x for x in obj['arguments']['fields'] if x['alias']!='COL_0']
        self.data['source_evidence']['sales'][0]['request']=self.save('sales-request.json',obj)
        with self.assertRaises(ValueError):ev.validate(self.source())
    def test_borrow_ended_reason_precedes_product(self):
        row=dict(module='borrow',head_id='H',detail_id='D',doc_no='1902-1',doc_date='2026-10-29',expected_date='2026-10-31',doc_type_name='类型',doc_type_code='1902',item_code='1-H1',item_name='DSN',columns={},borrow_detail_ended=True)
        self.data['records']=[row]
        result=plan.build_plan(self.data,'2026-10-29')
        self.assertEqual(result['excluded_details'][0]['reason'],'borrow_detail_ended')
    def test_void_and_invalid_approval(self):
        self.data['records'][0]['approval_status']='X'
        result=plan.build_plan(self.data,'2026-10-29')
        self.assertEqual(result['excluded_documents'][0]['reason'],'sales_void')
        self.assertEqual(result['excluded_documents'][0]['approval_status'],'X')
        for value in (None,'unknown'):
            self.data['records'][0]['approval_status']=value
            with self.assertRaises(ValueError):plan.build_plan(self.data,'2026-10-29')
        self.data['records'][0]['approval_status']='A'
        other=copy.deepcopy(self.data['records'][0]);other.update(detail_id='D2',approval_status='X')
        self.data['records'].append(other)
        with self.assertRaises(ValueError):plan.build_plan(self.data,'2026-10-29')
    def test_void_is_sales_only(self):
        for module,no in [('borrow','1902-1'),('outbound_application','1018-1')]:
            record=dict(module=module,head_id='H',detail_id='D',doc_no=no,doc_date='2026-10-29',expected_date='2026-10-31',doc_type_name='类型',doc_type_code='1902',item_code='A',item_name='正常',columns={},approval_status='X')
            if module=='borrow':record['borrow_detail_ended']=False
            self.data['records']=[record]
            self.assertEqual(plan.build_plan(self.data,'2026-10-29')['counts']['planned_rows'],1)
    def test_raw_approval_and_metadata_tampering(self):
        page=json.loads((self.dir/'sales.json').read_text(encoding='utf-8'));page['rows'][0]['APPROVAL_STATUS']='X'
        self.data['source_evidence']['sales'][0].update(self.save('sales.json',page))
        with self.assertRaises(ValueError):ev.validate(self.source())
        page['rows'][0]['APPROVAL_STATUS']='A';self.data['source_evidence']['sales'][0].update(self.save('sales.json',page))
        self.data['sales_approval_options']['X']='已审核'
        with self.assertRaises(ValueError):ev.validate(self.source())
if __name__=='__main__':unittest.main()
