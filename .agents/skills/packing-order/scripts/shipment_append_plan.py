"""Bind an audited shipment plan to a complete snapshot; no online operations."""
import argparse,csv,hashlib,io,json,re
from pathlib import Path

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def build(source,snapshot,authorized):
    if source.get('status')!='prepared_only' or source.get('anomalies'):raise ValueError('Need complete anomaly-free offline plan')
    if snapshot.get('schema_version')!=1 or not snapshot.get('ok') or not snapshot.get('complete') or snapshot.get('transport')!='dingtalk_sheet':raise ValueError('Need complete official sheet snapshot')
    if len(authorized)!=len(set(authorized)) or not authorized:raise ValueError('Explicit unique authorized sheets required')
    if source.get('allowed_write_sheets')!=authorized:raise ValueError('Authorized scope differs from source plan')
    if source.get('node_id')!=snapshot.get('node_id'):raise ValueError('Document identity mismatch')
    lookup={s['name']:s for s in snapshot['sheets']};requests=[];seen=set()
    def letters(n):
        out=''
        while n:n,i=divmod(n-1,26);out=chr(65+i)+out
        return out
    for s in source['sheets']:
        name=s['name']
        if name not in authorized or name in seen:raise ValueError('Unknown or repeated planned sheet')
        seen.add(name);before=lookup[name]
        if not before.get('scope_complete') or before.get('sheet_id')!=s['sheet_id']:raise ValueError('Sheet incomplete or identity changed')
        rows=list(csv.reader(io.StringIO(before['tsv']),delimiter='\t'));headers=rows[0]
        if headers!=s['headers'] or len(set(headers))!=len(headers):raise ValueError('Real header mismatch')
        if before['range']!=f'A1:{letters(len(headers))}{len(rows)}':raise ValueError('Snapshot matrix range mismatch')
        for field in ['预计发货日期','销货明细_业务数量']:
            if headers.count(field)!=1:raise ValueError('Missing or duplicate typed field '+field)
        values=s['values']
        if not values or len(values)!=len(s['source']):raise ValueError('Missing original detail provenance')
        if any(len(row)!=len(headers) or any(not isinstance(v,str) for v in row) for row in values):raise ValueError('Invalid planned matrix')
        requests.append({'sheet':name,'sheet_id':s['sheet_id'],'range':f'A{len(rows)+1}:{letters(len(headers))}{len(rows)+len(values)}','headers':headers,'values':values,'before_row_count':len(rows),'before_scope_complete':True,'type_columns':{'预计发货日期':{'type':'date','format':'yyyy-mm-dd'},'销货明细_业务数量':{'type':'number','format':'General'}},'detail_ids':[x['detail_id'] for x in s['source']]})
    return {'schema_version':1,'phase':'append','node_id':snapshot['node_id'],'target_url':snapshot['target_url'],'check_basis':'initial_complete_snapshot','requests':requests}

def response_value(raw):
    if raw.get('isError'):raise ValueError('Failed operation response')
    if 'structuredContent' in raw:return raw['structuredContent']
    if 'success' in raw:return raw
    return json.loads('\n'.join(x['text'] for x in raw.get('content',[]) if x.get('type')=='text'))

def project(planpath,receiptpath,snappath):
    plan,receipt,snapshot=read(planpath),read(receiptpath),read(snappath)
    if not receipt.get('ok') or receipt.get('failure') or receipt.get('phase')!='append':raise ValueError('Full successful typed append required')
    if receipt.get('input_binding',{}).get('plan_sha256')!=digest(planpath):raise ValueError('Receipt plan hash mismatch')
    if plan['snapshot_sha256']!=digest(snappath):raise ValueError('Snapshot hash mismatch')
    sourcepath=Path(plan['source_plan_path']);source=read(sourcepath)
    if digest(sourcepath)!=plan['source_plan_sha256']:raise ValueError('Source plan changed')
    rebuilt=build(source,snapshot,source['allowed_write_sheets'])
    if rebuilt['requests']!=plan['requests']:raise ValueError('Bound append plan differs from original full audited plan')
    started=read(Path(receiptpath).parent/'started.json')
    if started.get('plan')!=plan:raise ValueError('Started plan differs')
    operations=receipt.get('operations',[])
    if not (receipt['operation_count']==receipt['completed_operations']==len(operations)==len(started['operations'])):raise ValueError('Incomplete typed operation evidence')
    used=set()
    for i,(ref,expected) in enumerate(zip(operations,started['operations']),1):
        name=ref['file']
        if not re.fullmatch(r'operation-[0-9]{6}\.json',name) or name in used:raise ValueError('Invalid operation reference')
        used.add(name);actual=read(Path(receiptpath).parent/name)
        if actual.get('index')!=i or actual.get('request')!=expected:raise ValueError('Operation request/sequence mismatch')
        if response_value(actual['response']).get('success') is not True:raise ValueError('Operation not explicitly successful')
    lookup={s['name']:s for s in snapshot['sheets']}
    for req in plan['requests']:
        sheet=lookup[req['sheet']];rows=list(csv.reader(io.StringIO(sheet['tsv']),delimiter='\t'))+req['values']
        buf=io.StringIO(newline='');csv.writer(buf,delimiter='\t',lineterminator='\n').writerows(rows)
        sheet['tsv']=buf.getvalue();end=req['range'].split(':')[1];sheet['range']='A1:'+end;sheet['sheet_mcp']['row_indices']=list(range(1,len(rows)+1))
    snapshot['snapshot_basis']='local_receipt_projection'
    snapshot['projection']={'kind':'typed_shipment_append','original_snapshot':str(Path(snappath).resolve()),'original_snapshot_sha256':digest(snappath),'append_plan':str(Path(planpath).resolve()),'append_plan_sha256':digest(planpath),'receipt':str(Path(receiptpath).resolve()),'receipt_sha256':digest(receiptpath),'typed_cells':receipt['typed_cells'],'online_read_performed':False}
    return snapshot

def main():
    a=argparse.ArgumentParser();a.add_argument('--source-plan');a.add_argument('--snapshot',required=True);a.add_argument('--sheets');a.add_argument('--plan');a.add_argument('--receipt');a.add_argument('--out',required=True);args=a.parse_args()
    if args.receipt:
        if not args.plan or args.source_plan or args.sheets:raise ValueError('Projection requires --plan and --receipt only')
        out=Path(args.out).resolve()
        if out in [Path(x).resolve() for x in [args.plan,args.receipt,args.snapshot]]:raise ValueError('Cannot overwrite input')
        result=project(args.plan,args.receipt,args.snapshot)
        with out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
        print(json.dumps({'ok':True,'snapshot_basis':'local_receipt_projection','typed_cells':result['projection']['typed_cells'],'out':str(out)},ensure_ascii=False));return
    if not args.source_plan or not args.sheets or args.plan:raise ValueError('Preparation requires --source-plan and --sheets')
    out=Path(args.out).resolve();src=Path(args.source_plan).resolve();snap=Path(args.snapshot).resolve()
    if out in [src,snap]:raise ValueError('Cannot overwrite input')
    result=build(read(src),read(snap),args.sheets.split(','))
    result.update(source_plan_path=str(src),source_plan_sha256=digest(src),snapshot_path=str(snap),snapshot_sha256=digest(snap))
    with out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'ok':True,'rows':sum(len(x['values']) for x in result['requests']),'sheets':[x['sheet'] for x in result['requests']],'plan_file':str(out)},ensure_ascii=False))
if __name__=='__main__':main()
