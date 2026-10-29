"""Bound terminal output while preserving complete local command evidence."""
import json
from pathlib import Path
import time
from datetime import datetime, timezone
from local_evidence import digest_file, json_text, write_text, record


FIELDS = '''ok ready phase stage state job error_type document_no complete stopped automatic_retry saved
authenticated no_changes operation_id has_next_page include_detail
rows pages page_count count total prepared_count unique_groups order_selected
manual19_count manual19_unique_groups orders_created manual19_created
generated_count approved_count created approved confirmed already_confirmed
confirmed_count already_approved pending_count all_pending_count
completed_batches generated pending_total scope
output prepared_file prepared_sha256 pending pending_sha256 gate completion
creation_completion checkpoint failure_file details receipt plan
remaining_matching_and_picking_gate_required'''.split()


def print_json(value):
    text = json_text(value, ensure_ascii=False)
    record(model_output_bytes=len((text+'\n').encode('utf-8')))
    print(text, flush=True)


def compact(value):
    """Presentation only: no caller uses this projection to authorize business."""
    result = {}
    for key in FIELDS:
        item = value.get(key)
        if key in value and (item is None or type(item) in (bool, int, float) or
                             isinstance(item, str) and len(item) <= 1024):
            result[key] = item
    for key, limit in (('error', 600), ('note', 240)):
        if key in value:
            item = value[key]
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            result[key] = text[:limit] + ('…' if len(text) > limit else '')
    if value.get('failure'):
        failure = value['failure']
        result['failure'] = compact(failure) if isinstance(failure, dict) else str(failure)[:600]
        result['stopped'] = True
    if value.get('in_flight'):
        result['in_flight'] = compact(value['in_flight'])
        result['stopped'] = True
    for key, count_key in (('confirmed','confirmed_count'),('already_confirmed','already_confirmed_count'),
                           ('created','created_count'),('skipped','skipped_count'),
                           ('rows','row_count')):
        if isinstance(value.get(key), list):
            result[count_key] = len(value[key])
    return result


def write_new(path, value):
    # Exclusive creation: do not overwrite earlier result evidence.
    text = json_text(value, ensure_ascii=False, indent=2) + '\n'
    write_text(path, text, exclusive=True)
    return digest_file(path)


class SessionOutput:
    def __init__(self, base, session_id, emit=print_json, interval=30.0, clock=time.monotonic):
        self.directory = Path(base)/('session-output-'+session_id)
        self.directory.mkdir(exist_ok=False)
        self.emit, self.interval, self.clock = emit, interval, clock
        self.sequence, self.last_progress, self.suppressed = 0, None, 0

    def progress(self, value):
        # Keep every progress event, even those omitted from the terminal.
        value = {**value, 'observed_utc':datetime.now(timezone.utc).isoformat()}
        with (self.directory/'progress.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(value, ensure_ascii=False)+'\n')
        now = self.clock()
        urgent = value.get('ok') is False or bool(value.get('failure')) or bool(value.get('in_flight')) or value.get('state') == 'unknown'
        if urgent or self.last_progress is None or now-self.last_progress >= self.interval:
            self.emit({'event':'progress', **compact(value)})
            self.last_progress = now
        else:
            self.suppressed += 1

    def complete(self, value):
        self.sequence += 1
        path = self.directory/f'{self.sequence:06}.json'
        fingerprint = write_new(path, value)
        result = {**compact(value), 'event':'result', 'full_result':str(path),
                  'full_result_sha256':fingerprint, 'progress_omitted':self.suppressed}
        self.suppressed = 0
        self.emit(result)
        return result

    async def ready(self, session, discover_tools=False, capabilities=None):
        result = {'ready':True, 'tool_discovery':False, 'session_output':str(self.directory)}
        if capabilities is not None:
            result.update(tool_validation=True, validated_tool_count=len(capabilities),
                          capabilities_file=str(self.directory/'capabilities.json'))
        if discover_tools:
            if capabilities is None:
                response = await session.list_tools()
                manifest = {'names':[tool.name for tool in response.tools],
                            'has_next_page':bool(getattr(response,'nextCursor',None))}
            else:
                manifest = {'names':list(capabilities), 'has_next_page':False}
            path = self.directory/'tools.json'
            result.update(tool_discovery=True, tool_count=len(manifest['names']),
                          tools_file=str(path), tools_sha256=write_new(path,manifest),
                          tools_has_next_page=manifest['has_next_page'])
        self.emit(result)
        return result
