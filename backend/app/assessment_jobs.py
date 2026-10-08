"""Durable, owner-scoped model extraction jobs shared by all intake forms."""
from __future__ import annotations

import json
import logging
import asyncio

from . import db, service, model_usage
from .fact_extractor import extract_facts

log = logging.getLogger(__name__)
SCHEMA = """CREATE TABLE IF NOT EXISTS assessments(
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT,
    usage TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id,request_id));"""


def init_jobs():
    with db.connect() as c:
        c.executescript(SCHEMA)
        model_usage.init_schema(c)
        c.execute("UPDATE assessments SET status='failed',error='服务重启中断了评估，请重试；原申请已保留。',updated_at=? WHERE status='pending'", (db.now(),))


def _public(row):
    payload = json.loads(row['payload'])
    result = json.loads(row['result']) if row['result'] else {}
    return {'id': row['id'], 'status': row['status'], 'assessment': result.get('assessment'),
            'result': result or None, 'case_id': payload.get('case_id'), 'error': row['error'],
            'usage': json.loads(row['usage']) if row['usage'] else None,
            'created_at': row['created_at'], 'updated_at': row['updated_at']}


def get(user, job_id):
    with db.connect() as c:
        row = service.one(c, 'SELECT * FROM assessments WHERE id=?', (job_id,), '评估不存在')
        service.owner(user, row['user_id'])
        return _public(row)


def create(user, data):
    service.require_customer(user)
    request_id = service.request_key(data)
    with db.transaction() as c:
        existing = c.execute('SELECT * FROM assessments WHERE user_id=? AND request_id=?', (user['id'], request_id)).fetchone()
        if existing:
            return {**_public(existing), '_new': False}
        allowed = ('order_id', 'type', 'quantity', 'reason', 'condition_note', 'target_sku_id',
                   'reship_kind', 'part_sku_id', 'parent_case_id', 'attachment_ids', 'case_id', 'case_version')
        payload = {key: data[key] for key in allowed if key in data}
        if data.get('case_id'):
            case = service._case(c, user, data['case_id'])
            if case['status'] not in ('draft', 'needs_info', 'submitted'):
                service.fail(409, '该申请已经人工处理，历史建议不再覆盖。')
            if data.get('case_version', case['version']) != case['version']:
                service.fail(409, '申请已更新，请刷新后重新评估。')
            payload = {key: case[key] for key in allowed if key in case}
            payload.update(case_id=case['id'], case_version=case['version'])
        order = service._order(c, user, payload.get('order_id'))
        if payload.get('type') not in ('return_refund', 'exchange', 'reship', 'refund_only', 'repair', 'logistics'):
            service.fail(422, '请选择需要评估的售后类型。')
        service.positive(payload.get('quantity', 1))
        payload.setdefault('quantity', 1)
        for key in ('reason', 'condition_note'):
            if not isinstance(payload.get(key, ''), str) or len(payload.get(key, '')) > 4000:
                service.fail(422, '申请说明应为不超过4000字的文字。')
        if not payload.get('reason', '').strip():
            service.fail(422, '请说明希望处理的问题。')
        service._attachments(c, user, payload.get('attachment_ids', []))
        job_id, stamp = db.uid('assessment'), db.now()
        c.execute('INSERT INTO assessments VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (job_id, order['user_id'], request_id, 'pending', service.js(payload), None, None, None, stamp, stamp))
        return {**_public(c.execute('SELECT * FROM assessments WHERE id=?', (job_id,)).fetchone()), '_new': True}


async def run(job_id):
    try:
        with db.connect() as c:
            row = service.one(c, 'SELECT * FROM assessments WHERE id=?', (job_id,))
            if row['status'] != 'pending':
                return
            user = json.loads(service.one(c, 'SELECT data FROM users WHERE id=?', (row['user_id'],))[0])
            payload = json.loads(row['payload'])
            order = service._order(c, user, payload['order_id'])
            product = json.loads(service.one(c, 'SELECT data FROM products WHERE id=?', (order['product_id'],))[0])
        with model_usage.scope(job_id=job_id, user_id=user['id'], request_id=row['request_id'],
                               conversation_id=payload.get('conversation_id')):
            facts, usage = await extract_facts(payload, order, product)
        result = service.evaluate_after_sale(user, {**payload, 'facts': facts})
        status = 'completed'
        if payload.get('case_id'):
            updated = service.apply_assessment_result(user, payload['case_id'], payload['case_version'], facts,
                job_id=job_id, job_result=result, job_usage=usage)
            if updated is None:
                status = 'superseded'
            else:
                for key in ('assessment', 'facts', 'refund_cents', 'shipping_payer', 'first_notice_at'):
                    result[key] = updated[key]
        with db.transaction() as c:
            c.execute("UPDATE assessments SET status=?,result=?,usage=?,updated_at=? WHERE id=? AND status='pending'",
                      (status, service.js(result), service.js(usage), db.now(), job_id))
    except (Exception, asyncio.CancelledError) as exc:
        # Do not persist upstream bodies, URLs with credentials, or internal reasoning.
        log.warning('Assessment failed: type=%s job=%s', type(exc).__name__, job_id)
        detail = getattr(exc, 'detail', None)
        error = detail if isinstance(detail, str) else '本次评估暂时失败，申请已保留。请重试或联系人工。'
        if isinstance(exc, model_usage.BudgetExceeded):
            error = '本次验收模型预算已用尽，尚未完成评估；申请已保留。'
        if isinstance(exc, asyncio.CancelledError):
            error = '本次评估已中断，申请已保留。请重试或联系人工。'
        usage = model_usage.summary(job_id=job_id)
        with db.transaction() as c:
            c.execute("UPDATE assessments SET status='failed',error=?,usage=?,updated_at=? WHERE id=? AND status='pending'", (error, service.js(usage), db.now(), job_id))
        if isinstance(exc, asyncio.CancelledError):
            raise
