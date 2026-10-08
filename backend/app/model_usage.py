"""Payload-free provider attempt audit and optional shared, fail-closed budget.

Price ceiling verified 2026-09-23 at
https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ : Flash peak input
(cache hit/miss) 0.04/2 CNY/M, output 8 CNY/M. Reserve every input token
at the miss rate; settle confirmed complete cache counts at the hit rate.
These are conservative peak bounds, not the provider's final bill.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from . import db
from .config import settings
from . import campaign

_context = ContextVar('model_call_context', default={})
_META = ('turn_id', 'job_id', 'user_id', 'conversation_id', 'request_id')
_USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens',
          'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
_FLASH = {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'}
SCHEMA = """CREATE TABLE IF NOT EXISTS model_attempts(
 id TEXT PRIMARY KEY, turn_id TEXT, job_id TEXT, user_id TEXT,
 conversation_id TEXT, request_id TEXT, stage TEXT NOT NULL,
 retry_index INTEGER NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER,
 first_chunk_ms INTEGER, error_type TEXT, http_status INTEGER,
 usage TEXT, usage_status TEXT NOT NULL DEFAULT 'unknown',
 input_bound INTEGER, max_output_tokens INTEGER, reserved_micros INTEGER,
 cost_upper_micros INTEGER, budget_ledger TEXT, price_basis TEXT)"""


class BudgetExceeded(RuntimeError):
    """No provider request was issued because the shared budget was exhausted."""


class BudgetConfigurationError(RuntimeError):
    pass


def init_schema(connection):
    # execute, not executescript: never commit the caller's migration transaction.
    connection.execute(SCHEMA)
    if 'provider_model' not in {row[1] for row in connection.execute('PRAGMA table_info(model_attempts)')}:
        connection.execute('ALTER TABLE model_attempts ADD COLUMN provider_model TEXT')
    connection.execute('CREATE INDEX IF NOT EXISTS model_attempts_turn ON model_attempts(turn_id,created_at)')
    connection.execute('CREATE INDEX IF NOT EXISTS model_attempts_job ON model_attempts(job_id,created_at)')
    connection.execute('CREATE TABLE IF NOT EXISTS model_usage_rollups(scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(scope_type,scope_id))')


def recover(connection):
    """Startup-only recovery. Unknown/partial reservations remain fully held."""
    init_schema(connection)
    return connection.execute("UPDATE model_attempts SET status='interrupted',error_type='ProcessInterrupted',finished_at=?,usage_status=CASE WHEN usage IS NOT NULL THEN 'partial' ELSE 'unknown' END WHERE status IN ('prepared','running')", (db.now(),)).rowcount


@contextmanager
def scope(**metadata):
    values = dict(_context.get())
    values.update({k: str(v)[:200] for k, v in metadata.items() if k in _META and v is not None})
    token = _context.set(values)
    try:
        yield values
    finally:
        _context.reset(token)


def _money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise ValueError()
        return amount
    except Exception as exc:
        raise BudgetConfigurationError('Invalid positive budget/rate setting') from exc


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _pricing(model, enabled):
    input_rate = os.getenv('MODEL_BUDGET_INPUT_CNY_PER_M')
    output_rate = os.getenv('MODEL_BUDGET_OUTPUT_CNY_PER_M')
    if input_rate is not None or output_rate is not None:
        if input_rate is None or output_rate is None:
            raise BudgetConfigurationError('Both explicit input and output rates are required')
        return _money(input_rate), _money(output_rate), 'configured_peak_ceiling_cny_per_m'
    if model in _FLASH and settings.deepseek_base_url.rstrip('/') in ('https://api.deepseek.com', 'https://api.deepseek.com/v1'):
        return Decimal(2), Decimal(8), 'deepseek_flash_peak_cache_cny_2026-09-23'
    if enabled:
        raise BudgetConfigurationError('Unverified model/provider price; explicit ceilings are required')
    return None, None, 'unverified'


def _has_image(value):
    if isinstance(value, dict):
        return value.get('type') in ('image_url', 'input_image') or any(_has_image(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_image(v) for v in value)
    return False


def input_bound(body):
    """Text byte bound plus generous protocol framing; vision reserves full context.

    Tokenizer/image encoding isn't available locally. Never estimate a picture
    from its short URL. Flash's documented 1M context is the conservative bound.
    Only the resulting count is persisted, never the serialized request.
    """
    if _has_image(body.get('messages', [])):
        return max(1048576, int(os.getenv('MODEL_BUDGET_VISION_INPUT_BOUND', '1048576')))
    serialized = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return len(serialized) + 4096 + 256 * (len(body.get('messages', [])) + len(body.get('tools', [])))


@contextmanager
def _ledger(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('CREATE TABLE IF NOT EXISTS budget_limit(id INTEGER PRIMARY KEY CHECK(id=1), limit_micros INTEGER NOT NULL)')
        connection.execute("""CREATE TABLE IF NOT EXISTS budget_reservations(
            attempt_id TEXT PRIMARY KEY, reserved_micros INTEGER NOT NULL,
            held_micros INTEGER NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        connection.execute('BEGIN IMMEDIATE')
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def budget_snapshot(path=None):
    path = path or os.getenv('MODEL_BUDGET_LEDGER')
    if not path:
        return {'enabled': False}
    with _ledger(path) as connection:
        limit = connection.execute('SELECT limit_micros FROM budget_limit WHERE id=1').fetchone()
        held = connection.execute('SELECT COALESCE(SUM(held_micros),0) FROM budget_reservations').fetchone()[0]
        unknown = connection.execute("SELECT COUNT(*) FROM budget_reservations WHERE status NOT IN ('known','blocked')").fetchone()[0]
        return {'enabled': True, 'limit_micros': limit[0] if limit else None,
                'held_micros': held, 'uncertain_attempts': unknown,
                'remaining_micros': max(0, limit[0]-held) if limit else None}


def _reserve(path, attempt_id, limit, amount):
    with _ledger(path) as connection:
        existing = connection.execute('SELECT limit_micros FROM budget_limit WHERE id=1').fetchone()
        if existing and existing[0] != limit:
            raise BudgetConfigurationError('Shared ledger limit is immutable; do not reset an acceptance budget')
        connection.execute('INSERT OR IGNORE INTO budget_limit VALUES (1,?)', (limit,))
        used = connection.execute('SELECT COALESCE(SUM(held_micros),0) FROM budget_reservations').fetchone()[0]
        stage = None
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='campaign_stage'").fetchone():
            stage = connection.execute('SELECT ceiling_micros FROM campaign_stage WHERE id=1').fetchone()
        ceiling = min(limit, stage[0]) if stage else limit
        if used + amount > ceiling:
            raise BudgetExceeded('Model acceptance budget exhausted before sending request')
        stamp = db.now()
        connection.execute('INSERT INTO budget_reservations VALUES (?,?,?,?,?,?)', (attempt_id, amount, amount, 'reserved', stamp, stamp))


def _settle(path, attempt_id, cost, usage_status):
    with _ledger(path) as connection:
        row = connection.execute('SELECT * FROM budget_reservations WHERE attempt_id=?', (attempt_id,)).fetchone()
        if not row:
            raise BudgetConfigurationError('Missing budget reservation')
        held = cost if usage_status == 'known' else max(row['reserved_micros'], cost or 0)
        connection.execute('UPDATE budget_reservations SET held_micros=?,status=?,updated_at=? WHERE attempt_id=?',
                           (held, usage_status, db.now(), attempt_id))


class Attempt:
    def __init__(self, body, *, stage='generation', retry_index=0, **metadata):
        self.id = 'model_' + uuid4().hex
        self.metadata = {**_context.get(), **{k: str(v)[:200] for k, v in metadata.items() if k in _META and v is not None}}
        self.model = str(body.get('model', settings.deepseek_model))[:120]
        self.stage = stage if stage in ('generation', 'extraction', 'factcheck', 'evaluation') else 'generation'
        self.retry_index = retry_index
        self.body = body
        self.usage = {}
        self.complete_usage = False
        self.first_chunk_ms = None
        self.path = None
        self.reserved = None
        self.cost = None
        self.input_rate = self.output_rate = None
        self.cache_hit_rate = None
        self.cache_snapshot = None
        self.started = time.monotonic()

    def _write(self, **fields):
        with db.transaction() as connection:
            connection.execute('UPDATE model_attempts SET ' + ','.join(k+'=?' for k in fields) + ' WHERE id=?', (*fields.values(), self.id))

    def __enter__(self):
        with db.transaction() as connection:
            init_schema(connection)
            if self.metadata.get('turn_id') and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'").fetchone():
                turn = connection.execute('SELECT user_id,conversation_id FROM turns WHERE id=?', (self.metadata['turn_id'],)).fetchone()
                if turn:
                    self.metadata.update(user_id=turn[0], conversation_id=turn[1])
            connection.execute('INSERT INTO model_attempts(id,turn_id,job_id,user_id,conversation_id,request_id,stage,retry_index,model,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                               (self.id, *(self.metadata.get(k) for k in _META), self.stage, self.retry_index, self.model, 'prepared', db.now()))
        try:
            max_output = self.body.get('max_tokens')
            if type(max_output) is not int or max_output <= 0:
                raise BudgetConfigurationError('Every request must set a positive max_tokens')
            bound = input_bound(self.body)
            if bound <= 0:
                raise BudgetConfigurationError('Input token ceiling must be positive')
            configured = campaign.budget_environment()
            if configured and any(os.getenv(key) is not None for key in ('MODEL_BUDGET_INPUT_CNY_PER_M', 'MODEL_BUDGET_OUTPUT_CNY_PER_M')):
                raise BudgetConfigurationError('本次费用使用已核对的保守价格，不能覆盖单价。')
            limit_env = configured.get('MODEL_BUDGET_CNY', os.getenv('MODEL_BUDGET_CNY'))
            self.input_rate, self.output_rate, basis = _pricing(self.model, limit_env is not None)
            if basis == 'deepseek_flash_peak_cache_cny_2026-09-23':
                self.cache_hit_rate = Decimal('0.04')
            if self.input_rate is not None:
                self.reserved = _ceil(Decimal(bound)*self.input_rate + Decimal(max_output)*self.output_rate)
            if limit_env is not None:
                path = configured.get('MODEL_BUDGET_LEDGER', os.getenv('MODEL_BUDGET_LEDGER', ''))
                if not path or not Path(path).is_absolute():
                    raise BudgetConfigurationError('Budget requires one absolute shared MODEL_BUDGET_LEDGER path')
                self.path = str(Path(path).resolve())
                if self.path == str(Path(settings.db_path).resolve()):
                    raise BudgetConfigurationError('Shared budget ledger must be separate from the application database')
                _reserve(self.path, self.id, _ceil(_money(limit_env)*Decimal(1000000)), self.reserved)
            self._write(status='running', input_bound=bound, max_output_tokens=max_output,
                        reserved_micros=self.reserved, budget_ledger=self.path, price_basis=basis)
            return self
        except BaseException as exc:
            self._write(status='blocked_budget' if isinstance(exc, BudgetExceeded) else 'blocked_configuration',
                        error_type=type(exc).__name__, finished_at=db.now(), elapsed_ms=round((time.monotonic()-self.started)*1000))
            raise
        finally:
            # No payload retained on the audit object after sizing the request.
            self.body = None

    @property
    def usage_status(self):
        if self.complete_usage and 'prompt_tokens' in self.usage and 'completion_tokens' in self.usage:
            return 'known'
        return 'partial' if self.usage else 'unknown'

    def observe_usage(self, usage, *, final=False):
        if not isinstance(usage, dict):
            return
        # Validate the supplier's single complete snapshot, before sanitizing or
        # merging counters. Missing/invalid late fields must not inherit a prior
        # cache discount or manufacture a balanced breakdown across chunks.
        cache_keys = ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
        self.cache_snapshot = None
        if all(type(usage.get(key)) is int and usage[key] >= 0 for key in cache_keys):
            if usage['prompt_cache_hit_tokens'] + usage['prompt_cache_miss_tokens'] == usage['prompt_tokens']:
                self.cache_snapshot = {key: usage[key] for key in cache_keys}
        for key in _USAGE:
            if type(usage.get(key)) is int and usage[key] >= 0:
                self.usage[key] = max(self.usage.get(key, 0), usage[key])
        if not self.usage:
            return
        self.complete_usage = self.complete_usage or final
        self._update_cost()
        self._write(usage=json.dumps(self.usage), usage_status=self.usage_status, cost_upper_micros=self.cost)
        # Retain the reservation until exit: a partial stream can still grow.

    def _update_cost(self):
        # No observed billable counters means unknown, not a zero-priced call.
        # A total-only counter cannot be priced without the input/output split.
        # The independent reservation remains fully held for unknown usage.
        if self.input_rate is None or not any(key in self.usage for key in ('prompt_tokens', 'completion_tokens')):
            self.cost = None
            return
        input_cost = Decimal(self.usage.get('prompt_tokens', 0)) * self.input_rate
        snapshot = self.cache_snapshot
        if (self.usage_status == 'known' and self.cache_hit_rate is not None and snapshot
                and all(snapshot[key] == self.usage.get(key) for key in snapshot)):
            input_cost = (Decimal(snapshot['prompt_cache_hit_tokens']) * self.cache_hit_rate
                          + Decimal(snapshot['prompt_cache_miss_tokens']) * self.input_rate)
        self.cost = _ceil(input_cost + Decimal(self.usage.get('completion_tokens', 0)) * self.output_rate)

    def mark_first_chunk(self):
        if self.first_chunk_ms is None:
            self.first_chunk_ms = round((time.monotonic()-self.started)*1000)
            self._write(first_chunk_ms=self.first_chunk_ms)

    def observe_model(self, name):
        """Provider-reported identifier only; absent does not imply a version."""
        if isinstance(name, str) and 0 < len(name) <= 120 and all(char.isalnum() or char in '._:/-' for char in name):
            self._write(provider_model=name)

    def __exit__(self, exc_type, exc, tb):
        status = 'succeeded' if exc_type is None else 'cancelled' if issubclass(exc_type, asyncio.CancelledError) or exc_type.__name__ == 'Superseded' else 'failed'
        self.complete_usage = False if status == 'cancelled' else self.complete_usage or exc_type is None
        self._update_cost()
        response = getattr(exc, 'response', None)
        http_status = getattr(response, 'status_code', None)
        self._write(status=status, finished_at=db.now(), elapsed_ms=round((time.monotonic()-self.started)*1000),
                    error_type=exc_type.__name__ if exc_type else None,
                    usage_status=self.usage_status, cost_upper_micros=self.cost,
                    http_status=http_status if type(http_status) is int else None)
        if self.path:
            _settle(self.path, self.id, self.cost, self.usage_status)
        return False


def attempt(body, **kwargs):
    return Attempt(body, **kwargs)


def _aggregate(rows):
    usage = [json.loads(row['usage']) if row['usage'] else {} for row in rows]
    known = {key: sum(item.get(key, 0) for item in usage) for key in _USAGE if any(key in item for item in usage)}
    return {'requests': sum(row['status'] not in ('blocked_budget','blocked_configuration','prepared') for row in rows),
            'attempts': len(rows), 'unknown_attempts': sum(row['usage_status'] != 'known' and row['status'] not in ('blocked_budget','blocked_configuration') for row in rows),
            'known_usage': known, 'statuses': {status: sum(row['status']==status for row in rows) for status in {row['status'] for row in rows}}}


def _combine(left, right):
    result = {key: left.get(key, 0)+right.get(key, 0) for key in ('requests', 'attempts', 'unknown_attempts')}
    for field in ('known_usage', 'statuses'):
        a, b = left.get(field, {}), right.get(field, {})
        result[field] = {key: a.get(key, 0)+b.get(key, 0) for key in a.keys() | b.keys()}
    result['usage_status'] = 'known' if result['requests'] and not result['unknown_attempts'] else 'partial' if result['known_usage'] else 'unknown'
    return result


def summary(*, turn_id=None, job_id=None):
    """Internal caller must enforce ownership before exposing a scoped summary."""
    if not turn_id and not job_id:
        raise ValueError('An explicit turn/job scope is required')
    field, value = ('turn_id', turn_id) if turn_id else ('job_id', job_id)
    with db.connect() as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_attempts'").fetchone()
        rows = connection.execute('SELECT * FROM model_attempts WHERE '+field+'=? ORDER BY created_at,id', (value,)).fetchall() if exists else []
        rollups = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_usage_rollups'").fetchone()
        archived = connection.execute('SELECT data FROM model_usage_rollups WHERE scope_type=? AND scope_id=?', (field, value)).fetchone() if rollups else None
    return _combine(json.loads(archived[0]) if archived else {}, _aggregate(rows))


def prune(connection, retention_days=None):
    """Archive counts before deleting expired terminal attempts, atomically.

    The shared money ledger, unfinished calls, turns/jobs and business audits
    are never deleted here. Historical scoped summaries remain queryable.
    """
    retention_days = int(os.getenv('MODEL_USAGE_RETENTION_DAYS', '30')) if retention_days is None else retention_days
    if type(retention_days) is not int or retention_days < 1:
        raise ValueError('retention_days must be a positive integer')
    init_schema(connection)
    connection.execute('SAVEPOINT model_usage_prune')
    try:
        cutoff = (datetime.now(timezone.utc)-timedelta(days=retention_days)).isoformat()
        rows = connection.execute("SELECT * FROM model_attempts WHERE finished_at<? AND status NOT IN ('prepared','running')", (cutoff,)).fetchall()
        groups = {('all', 'all'): list(rows)} if rows else {}
        for row in rows:
            for field in ('turn_id', 'job_id'):
                if row[field]: groups.setdefault((field, row[field]), []).append(row)
        for (scope_type, scope_id), selected in groups.items():
            old = connection.execute('SELECT data FROM model_usage_rollups WHERE scope_type=? AND scope_id=?', (scope_type, scope_id)).fetchone()
            combined = _combine(json.loads(old[0]) if old else {}, _aggregate(selected))
            connection.execute('INSERT INTO model_usage_rollups VALUES (?,?,?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at', (scope_type, scope_id, json.dumps(combined), db.now()))
        connection.executemany('DELETE FROM model_attempts WHERE id=?', [(row['id'],) for row in rows])
        connection.execute('RELEASE model_usage_prune')
        return len(rows)
    except BaseException:
        connection.execute('ROLLBACK TO model_usage_prune')
        connection.execute('RELEASE model_usage_prune')
        raise
