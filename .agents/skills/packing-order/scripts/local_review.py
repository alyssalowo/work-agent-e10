#!/usr/bin/env python3
"""Offline workorder-source checks and evidence indexing; no business actions."""
import argparse
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import sys

FILTER_KEYS = {'doc_no_prefix','date_from','excluded_approval_status','excluded_statuses'}
FIELDS = ('单号','需求单号','产品品号','特征码','预计产量')
KEYS = ('DOC_NO','DEMAND_NO','ITEM_CODE','ITEM_FEATURE_CODE','PLAN_QTY')
REQUIRED = {'ID','DOC_DATE','APPROVAL_STATUS','STATUS',*KEYS}
PURPOSES = {'pre_picking_inventory','conflict_evidence','final_report'}


def need(condition, message):
    if not condition: raise ValueError(message)


def sha(data): return hashlib.sha256(data).hexdigest()


def parse(data):
    return json.loads(data.decode('utf-8-sig'),parse_float=Decimal,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('无效数值：'+value)))


def read_manifest(path):
    path=Path(path).resolve(); data=path.read_bytes(); value=parse(data)
    need(value.get('schema_version')==1 and isinstance(value.get('task_id'),str) and value['task_id'].strip(), '缺少本轮schema_version/task_id')
    return path,value,{path:sha(data)}


def input_file(manifest_path, item, frozen):
    need(isinstance(item,dict) and isinstance(item.get('path'),str) and item['path'], '输入文件路径缺失')
    expected=item.get('sha256')
    need(isinstance(expected,str) and len(expected)==64 and all(c in '0123456789abcdef' for c in expected), '输入SHA256缺失或无效')
    path=(manifest_path.parent/item['path']).resolve(); data=path.read_bytes()
    need(sha(data)==expected, '输入文件已变化：'+str(path))
    frozen[path]=expected
    return path,data


def publish(directory, frozen, files):
    root=Path(directory).resolve(); skill=Path(__file__).resolve().parents[1]
    need(not root.is_relative_to(skill) and '.agents' not in {p.casefold() for p in root.parts}, '输出不能放在技能目录')
    targets=[root/name for name in files]
    need(not any(p in frozen for p in targets), '输出不能覆盖输入')
    need(not any(p.exists() for p in targets), '输出已存在，保留原产物，不覆盖或重放')
    for path,expected in frozen.items():need(sha(path.read_bytes())==expected,'处理期间输入已变化')
    root.mkdir(parents=True,exist_ok=True)
    result={}
    for name,content in files.items():
        path=root/name
        with path.open('x',encoding='utf-8',newline='') as output:output.write(content)
        result[name]={'path':str(path),'sha256':sha(path.read_bytes())}
    return result


def json_text(value): return json.dumps(value,ensure_ascii=False,indent=2)+'\n'


def workorders(manifest_path, directory):
    path,manifest,frozen=read_manifest(manifest_path)
    filters=manifest.get('filters',{})
    need(set(filters)==FILTER_KEYS, '须明确指定五项AND条件，不添加人员/工厂/本轮限制')
    need(isinstance(filters['doc_no_prefix'],str) and filters['doc_no_prefix'].strip(), '单号前缀缺失')
    start_date=date.fromisoformat(filters['date_from'])
    need(filters['date_from']==start_date.isoformat() and filters['excluded_approval_status']=='V' and filters['excluded_statuses']==['y','Y'], '日期或原生状态条件不符；保留y/Y大小写')
    pages=manifest.get('pages');need(isinstance(pages,list) and bool(pages),'缺少完整分页清单')
    rows=[]; ids=set(); numbers=set(); seen_paths=set(); start=1; complete=False; missing=[]
    for item in pages:
        need(not complete,'结束页后仍有额外分页，不能混用查询')
        page_path,data=input_file(path,item,frozen)
        need(page_path not in seen_paths,'重复分页文件');seen_paths.add(page_path)
        packet=parse(data)
        need(packet.get('ok') is True and packet.get('filters')==filters,'查询失败或筛选条件不符')
        need(type(packet.get('start_row')) is int and packet['start_row']==start,'分页起点不连续')
        page_rows=packet.get('rows');need(isinstance(page_rows,list),'缺少分页记录')
        for row in page_rows:
            need(isinstance(row,dict) and REQUIRED<=row.keys(),'查询返回字段缺失，不能猜作空特征或空数量')
            ident,number=row['ID'],row['DOC_NO']
            need(isinstance(ident,str) and bool(ident.strip()) and isinstance(number,str) and number.startswith(filters['doc_no_prefix']),'工单身份或前缀不符')
            need(ident not in ids and number not in numbers,'工单ID或单号重复，分页未通过')
            need(isinstance(row['DOC_DATE'],str) and date.fromisoformat(row['DOC_DATE'][:10])>=start_date,'工单日期不符')
            need(isinstance(row['APPROVAL_STATUS'],str) and row['APPROVAL_STATUS']!='V' and isinstance(row['STATUS'],str) and row['STATUS'] not in ('y','Y'),'作废或完工状态不符')
            need(all(row[key] is None or isinstance(row[key],str) for key in KEYS[1:4]),'匹配文本字段类型无效')
            values=['' if row[key] is None else str(row[key]) for key in KEYS]
            if values[-1].strip():
                try: quantity=Decimal(values[-1])
                except InvalidOperation:raise ValueError('预计产量不是精确数值：'+number)
                need(type(row['PLAN_QTY']) is not bool and quantity.is_finite(),'预计产量无效：'+number)
            absent=[FIELDS[i] for i in (1,2,4) if not values[i].strip()]
            if absent:missing.append({'document_no':number,'missing_fields':absent})
            rows.append(values);ids.add(ident);numbers.add(number)
        expected=start+len(page_rows)
        need(type(packet.get('next_start_row')) is int and packet['next_start_row']==expected,'分页结束位置不符')
        more=packet.get('has_next_page')
        need(more is True or more is False,'分页结束状态未知')
        need(more is False or bool(page_rows),'空分页无可靠进展')
        complete=more is False;start=expected
    need(complete,'分页尚未完整结束')
    buffer=io.StringIO(newline='');writer=csv.writer(buffer,delimiter='\t');writer.writerow(FIELDS);writer.writerows(rows)
    summary={'ok':True,'kind':'workorder_source','task_id':manifest['task_id'],'complete':True,'has_next_page':False,
             'filters':filters,'rows':len(rows),'page_count':len(pages),'missing_field_records':len(missing),
             'missing_fields':missing,'input_sha256':{str(p):v for p,v in frozen.items()},
             'source':'workorder-source.tsv','business_gate':'not_evaluated','main_agent_review_required':True}
    files=publish(directory,frozen,{'workorder-source.tsv':buffer.getvalue(),'source-summary.json':json_text(summary)})
    return {'ok':True,'task_id':manifest['task_id'],'rows':len(rows),'pages':len(pages),
            'missing_field_records':len(missing),'business_gate':'not_evaluated','outputs':files}


def pointer(value, expression):
    need(isinstance(expression,str) and (expression=='' or expression.startswith('/')),'JSON Pointer格式无效')
    if expression=='':return value
    for raw in expression[1:].split('/'):
        # Only RFC 6901 escapes; reject misspellings instead of selecting a wrong field.
        need(all(raw[i+1:i+2] in ('0','1') for i,c in enumerate(raw) if c=='~'),'JSON Pointer转义无效')
        key=raw.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            need(key=='0' or key.isascii() and key.isdigit() and not key.startswith('0'),'JSON Pointer列表下标无效')
            value=value[int(key)]
        else:
            need(isinstance(value,dict) and key in value,'JSON Pointer字段缺失：'+expression)
            value=value[key]
    return value


def describe(value):
    if isinstance(value,(dict,list)):
        return {'value_type':'object' if isinstance(value,dict) else 'array','item_count':len(value)}
    if isinstance(value,Decimal):return {'value_type':'number','number_text':str(value)}
    return {'value_type':type(value).__name__,'value':value}


def evidence(manifest_path, directory):
    path,manifest,frozen=read_manifest(manifest_path)
    purpose=manifest.get('purpose');need(purpose in PURPOSES,'证据任务用途无效')
    inputs=manifest.get('inputs');need(isinstance(inputs,list) and bool(inputs),'缺少主代理指定的证据文件')
    index=[]
    for item in inputs:
        need(isinstance(item.get('role'),str) and item['role'].strip(),'证据角色缺失')
        source,data=input_file(path,item,frozen)
        record={'role':item['role'],'path':str(source),'sha256':sha(data),'bytes':len(data)}
        if source.suffix.lower()=='.json':
            value=parse(data);pointers=item.get('pointers',[])
            need(isinstance(pointers,list) and len(set(pointers))==len(pointers),'JSON Pointer列表无效或重复')
            record.update(format='json',locations=[{'pointer':p,**describe(pointer(value,p))} for p in pointers])
        else:
            need(source.suffix.lower() in ('.csv','.tsv') and not item.get('pointers'),'证据只支持JSON/CSV/TSV，CSV不使用JSON Pointer')
            rows=list(csv.reader(io.StringIO(data.decode('utf-8-sig')),delimiter='\t' if source.suffix.lower()=='.tsv' else ',',strict=True))
            record.update(format=source.suffix.lower()[1:],header=rows[0] if rows else [],record_count=max(0,len(rows)-1))
        index.append(record)
    result={'ok':True,'task_id':manifest['task_id'],'purpose':purpose,'business_gate':'not_evaluated',
            'main_agent_review_required':True,'input_sha256':{str(p):v for p,v in frozen.items()},'files':index}
    files=publish(directory,frozen,{'evidence-index.json':json_text(result)})
    return {'ok':True,'task_id':manifest['task_id'],'purpose':purpose,'indexed_files':len(index),
            'business_gate':'not_evaluated','outputs':files}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('workorders','evidence'):
        command=sub.add_parser(name);command.add_argument('--manifest',required=True);command.add_argument('--out-dir',required=True)
    args=parser.parse_args()
    try:result=globals()[args.command](args.manifest,args.out_dir)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc),'business_gate':'not_evaluated','automatic_retry':False},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':sys.exit(main())
