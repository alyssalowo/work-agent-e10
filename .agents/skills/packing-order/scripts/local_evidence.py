"""Operation-local evidence and optional aggregate timings; no business payload logging."""
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import subprocess
import time

_active = ContextVar('packing_local_profile', default=None)
_FIELDS = ('file_reads', 'file_bytes', 'file_read_seconds', 'file_writes',
           'file_write_bytes', 'file_write_seconds', 'repeated_file_reads',
           'repeated_file_bytes', 'sha256_calls', 'sha256_bytes', 'sha256_seconds',
           'json_parses', 'json_parse_seconds', 'json_serializations',
           'json_serialize_seconds', 'subprocess_calls', 'subprocess_seconds',
           'mcp_calls', 'mcp_wait_seconds', 'model_output_bytes')


class Profile:
    """Only numeric totals leave this object. Paths are kept in memory until exit."""
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.counters = dict.fromkeys(_FIELDS, 0)
        self._reads = set()

    def __enter__(self):
        self.started = time.perf_counter()
        self._token = _active.set(self if self.enabled else None)
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self.started
        _active.reset(self._token)
        self._reads.clear()

    def snapshot(self):
        return {**{k: round(v, 9) if isinstance(v, float) else v
                   for k, v in self.counters.items()},
                'elapsed_seconds': round(getattr(self, 'elapsed', time.perf_counter()-self.started), 9)}


def record(**counts):
    profile = _active.get()
    if profile is not None:
        for key, amount in counts.items():
            profile.counters[key] += amount


def read_bytes(path):
    profile = _active.get()
    if profile is None:
        return Path(path).read_bytes()
    path = Path(path)
    tick = time.perf_counter()
    data = path.read_bytes()
    record(file_reads=1, file_bytes=len(data), file_read_seconds=time.perf_counter()-tick)
    key = path.resolve()
    if key in profile._reads:
        record(repeated_file_reads=1, repeated_file_bytes=len(data))
    profile._reads.add(key)
    return data


def sha256_bytes(data):
    profile = _active.get()
    tick = time.perf_counter() if profile is not None else 0
    result = hashlib.sha256(data).hexdigest()
    if profile is not None:
        record(sha256_calls=1, sha256_bytes=len(data), sha256_seconds=time.perf_counter()-tick)
    return result


def digest_file(path):
    """Hash-only inputs use bounded memory; never trust size or mtime as evidence."""
    profile = _active.get()
    fingerprint = hashlib.sha256()
    total = 0
    read_seconds = hash_seconds = 0.0
    with Path(path).open('rb') as stream:
        block_size = 8192
        while True:
            tick = time.perf_counter() if profile is not None else 0
            chunk = stream.read(block_size)
            if profile is not None:
                read_seconds += time.perf_counter()-tick
            if not chunk:
                break
            total += len(chunk)
            tick = time.perf_counter() if profile is not None else 0
            fingerprint.update(chunk)
            if profile is not None:
                hash_seconds += time.perf_counter()-tick
            if len(chunk) == block_size:
                block_size = min(block_size * 2, 1024 * 1024)
    if profile is not None:
        record(file_reads=1, file_bytes=total, file_read_seconds=read_seconds,
               sha256_calls=1, sha256_bytes=total, sha256_seconds=hash_seconds)
        key = Path(path).resolve()
        if key in profile._reads:
            record(repeated_file_reads=1, repeated_file_bytes=total)
        profile._reads.add(key)
    return fingerprint.hexdigest()


def json_bytes(raw, **kwargs):
    profile = _active.get()
    tick = time.perf_counter() if profile is not None else 0
    try:
        return json.loads(raw.decode('utf-8-sig'), **kwargs)
    finally:
        if profile is not None:
            record(json_parses=1, json_parse_seconds=time.perf_counter()-tick)


def read_json(path):
    return json_bytes(read_bytes(path))


def json_text(value, **kwargs):
    profile = _active.get()
    tick = time.perf_counter() if profile is not None else 0
    try:
        return json.dumps(value, **kwargs)
    finally:
        if profile is not None:
            record(json_serializations=1, json_serialize_seconds=time.perf_counter()-tick)


def write_text(path, text, *, exclusive=False):
    profile = _active.get()
    tick = time.perf_counter() if profile is not None else 0
    with Path(path).open('x' if exclusive else 'w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    if profile is not None:
        record(file_writes=1, file_write_bytes=len(text.encode('utf-8')),
               file_write_seconds=time.perf_counter()-tick)


def run_subprocess(*args, **kwargs):
    """Measure the existing launch and wait together; never log its arguments."""
    profile = _active.get()
    tick = time.perf_counter() if profile is not None else 0
    try:
        return subprocess.run(*args, **kwargs)
    finally:
        if profile is not None:
            record(subprocess_calls=1, subprocess_seconds=time.perf_counter()-tick)


class VerifiedInputs:
    """A private synchronous validation pass; discard before awaiting external work.

    Retain bytes only for files the caller will parse. Hash-only evidence streams.
    No caller may inject a precomputed digest or reuse this across operations.
    """
    def __init__(self, keep=()):
        self._paths = {}
        self._keep = {self._path(p) for p in keep}
        self._data, self._hashes, self._json = {}, {}, {}

    def _path(self, path):
        path = Path(path)
        if path not in self._paths:
            resolved = path.resolve()
            self._paths[path] = resolved
            self._paths[resolved] = resolved
        return self._paths[path]

    def verify(self, path, expected, message):
        path = self._path(path)
        if path not in self._hashes:
            if path in self._keep:
                raw = read_bytes(path)
                self._hashes[path] = sha256_bytes(raw)
                self._data[path] = raw
            else:
                self._hashes[path] = digest_file(path)
        if self._hashes[path] != expected:
            raise ValueError(message)

    def fingerprint(self, path):
        return self._hashes[self._path(path)]

    def json(self, path):
        path = self._path(path)
        if path not in self._json:
            self._json[path] = json_bytes(self._data[path])
        return self._json[path]
