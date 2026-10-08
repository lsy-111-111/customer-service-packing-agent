"""Employee follow-up metadata and atomic, previewed replies; no fulfillment."""
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from . import db, service, support

SCHEMA = '''
CREATE TABLE IF NOT EXISTS work_operators(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,linked_user_id TEXT UNIQUE,active INTEGER NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_operations(target_type TEXT NOT NULL,target_id TEXT NOT NULL,conversation_id TEXT,version INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(target_type,target_id));
CREATE TABLE IF NOT EXISTS work_followup_anchors(target_type TEXT NOT NULL,target_id TEXT NOT NULL,conversation_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(target_type,target_id));
CREATE TABLE IF NOT EXISTS work_operation_history(id TEXT PRIMARY KEY,target_type TEXT NOT NULL,target_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_action_previews(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,expires_at TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS work_operation_receipts(actor_id TEXT NOT NULL,operation TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(actor_id,operation,request_id));
CREATE INDEX IF NOT EXISTS work_operations_conversation ON work_operations(conversation_id);
'''
TITLES = {'register_shortage': '登记少发', 'wait_photos': '等待照片', 'transfer_after_sale': '转交售后'}
FIELDS = {'assignee_id': '跟进负责人', 'next_step': '下一步', 'reason': '跟进原因', 'order_id': '关联订单', 'missing_item': '缺少的物品', 'missing_quantity': '缺少数量', 'source_kind': '陈述来源', 'source_message_id': '顾客原话', 'source_quote': '引用原文', 'photo_requirements': '需要补充的照片', 'materials_reviewed': '已核对现有材料', 'evidence_attachment_ids': '已核对的附件'}


def init_schema(c):
    # execute each statement: executescript would commit an existing transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip(): c.execute(statement)


def _json(v): return json.dumps(v, ensure_ascii=False, sort_keys=True)
def _hash(v): return hashlib.sha256(_json(v).encode()).hexdigest()
def _fail(code, text): raise HTTPException(code, text)
def _staff(user): service.require_staff(user)
def _actor(user): return {'id': user['id'], 'name': user.get('name', '店铺客服')}
def _text(v, label, maximum=500, required=True): return support._text(v, label, maximum, required)


@contextmanager
def _read():
    with db.connect() as c:
        c.execute('BEGIN')
        yield c


def _keys(data, allowed):
    if not isinstance(data, dict): _fail(422, '请填写有效的表单内容')
    if set(data) - set(allowed): _fail(422, '表单包含不支持的内容，请刷新后重试')


def _version(value):
    if type(value) is not int or value < 0: _fail(422, '请刷新待办后重新保存')
    return value


def _time(value, label):
    if value is None: return None
    if not isinstance(value, str): _fail(422, f'请填写有效的{label}')
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None: raise ValueError()
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError): _fail(422, f'{label}需包含时区，请重新选择时间')


def seed_default_operators(c):
    for row in c.execute('SELECT data FROM users'):
        user = json.loads(row[0])
        if user.get('role') != 'staff': continue
        if c.execute('SELECT 1 FROM work_operators WHERE linked_user_id=?', (user['id'],)).fetchone(): continue
        name = user.get('name') or '店铺客服'; base = name; index = 2
        while c.execute('SELECT 1 FROM work_operators WHERE name=?', (name,)).fetchone():
            name = f'{base}（{index}）'; index += 1
        stamp = db.now(); v = {'id': db.uid('operator'), 'name': name, 'linked_user_id': user['id'], 'active': True, 'version': 1, 'note': '现有客服的内部责任登记', 'created_at': stamp, 'updated_at': stamp}
        c.execute('INSERT INTO work_operators VALUES (?,?,?,?,?,?)', (v['id'], name, user['id'], 1, 1, _json(v)))


def _operator(c, identity, active=False):
    row = c.execute('SELECT data FROM work_operators WHERE id=?', (identity,)).fetchone() if isinstance(identity, str) else None
    if not row: _fail(422, '请选择已登记的跟进负责人')
    v = json.loads(row[0])
    if active and not v['active']: _fail(409, '该负责人已停用，请重新选择')
    return v


def _memo(c, user, operation, data):
    rid = _text(data.get('request_id'), '提交凭证', 200); digest = _hash(data)
    row = c.execute('SELECT * FROM work_operation_receipts WHERE actor_id=? AND operation=? AND request_id=?', (user['id'], operation, rid)).fetchone()
    if row and row['fingerprint'] != digest: _fail(409, '同一提交凭证不能用于不同内容，请刷新后重试')
    return rid, digest, json.loads(row['data']) if row else None


def _remember(c, user, operation, rid, digest, result):
    c.execute('INSERT INTO work_operation_receipts VALUES (?,?,?,?,?)', (user['id'], operation, rid, digest, _json(result)))
    return result


def list_operators(user, include_inactive=False):
    _staff(user)
    with _read() as c:
        items = [json.loads(r[0]) for r in c.execute('SELECT data FROM work_operators ORDER BY rowid')]
    return {'items': [v for v in items if include_inactive or v['active']], 'suggested_operator_id': next((v['id'] for v in items if v['active'] and v.get('linked_user_id') == user['id']), None), 'notice': '姓名仅用于内部责任登记，不创建登录账号，也不表示已通知该同事。'}


def create_operator(user, data):
    _staff(user); _keys(data, ['name', 'note', 'request_id'])
    with db.transaction() as c:
        rid, digest, old = _memo(c, user, 'create_operator', data)
        if old: return old
        name = _text(data.get('name'), '真实同事姓名', 80); note = _text(data.get('note', ''), '备注', 500, False)
        if c.execute('SELECT 1 FROM work_operators WHERE name=?', (name,)).fetchone(): _fail(409, '该姓名已登记，请选择已有负责人')
        stamp = db.now(); value = {'id': db.uid('operator'), 'name': name, 'active': True, 'version': 1, 'linked_user_id': None, 'note': note, 'created_at': stamp, 'updated_at': stamp}
        c.execute('INSERT INTO work_operators VALUES (?,?,?,?,?,?)', (value['id'], name, None, 1, 1, _json(value)))
        service.audit(c, user, '登记跟进负责人', value['id'], None, value)
        return _remember(c, user, 'create_operator', rid, digest, value)


def update_operator(user, identity, data):
    _staff(user); _keys(data, ['name', 'active', 'note', 'reason', 'expected_version', 'request_id'])
    with db.transaction() as c:
        rid, digest, old = _memo(c, user, 'operator:' + identity, data)
        if old: return old
        value = _operator(c, identity); before = dict(value)
        if _version(data.get('expected_version')) != value['version']: _fail(409, '负责人资料已更新，请刷新后重试')
        reason = _text(data.get('reason'), '变更原因')
        if 'name' in data:
            name = _text(data['name'], '真实同事姓名', 80)
            if c.execute('SELECT 1 FROM work_operators WHERE name=? AND id<>?', (name, identity)).fetchone(): _fail(409, '该姓名已登记，请使用其他姓名')
            value['name'] = name
        if 'active' in data:
            if type(data['active']) is not bool: _fail(422, '请选择是否启用负责人')
            if not data['active'] and any((i['operations'].get('assignee') or {}).get('id') == identity for i in _all_items(c, user)):
                _fail(409, '该负责人仍有未完待办，请先完成转派')
            value['active'] = data['active']
        if 'note' in data: value['note'] = _text(data['note'], '备注', 500, False)
        value.update(version=value['version'] + 1, updated_at=db.now())
        c.execute('UPDATE work_operators SET name=?,active=?,version=?,data=? WHERE id=?', (value['name'], int(value['active']), value['version'], _json(value), identity))
        service.audit(c, user, '修改负责人资料', identity, before, {'operator': value, 'reason': reason})
        return _remember(c, user, 'operator:' + identity, rid, digest, value)


def _target_key(target):
    _keys(target, ['type', 'id'])
    if target.get('type') not in ['case', 'service_request', 'conversation', 'logistics_exception']: _fail(422, '请选择有效的待办事项')
    return target['type'], _text(target.get('id'), '待办事项', 500)


def _stored(c, item):
    row = c.execute('SELECT data FROM work_operations WHERE target_type=? AND target_id=?', (item['type'], item['id'])).fetchone()
    return json.loads(row[0]) if row else None


def _operations(c, item):
    value = _stored(c, item)
    if value:
        value = {k: v for k, v in value.items() if k != 'anchor'}
        if value.get('assignee'): value['assignee'] = _operator(c, value['assignee']['id'])
    else:
        linked = c.execute('SELECT data FROM work_operators WHERE linked_user_id=?', (item.get('assigned_staff_id'),)).fetchone() if item.get('assigned_staff_id') else None
        value = {'version': 0, 'assignee': json.loads(linked[0]) if linked else None, 'next_step': '', 'staff_due_at': None, 'platform_deadline': None, 'customer_wait': {'waiting': False, 'since': None, 'reason': '', 'actor': None}, 'followup_state': 'open', 'updated_at': None, 'registrations': []}
    value['history'] = [json.loads(r[0]) for r in c.execute('SELECT data FROM work_operation_history WHERE target_type=? AND target_id=? ORDER BY rowid', (item['type'], item['id']))]
    return value


def _enrich(c, item):
    item = dict(item); item['operations'] = _operations(c, item)
    if item['type'] == 'conversation' and not item.get('needs_handoff'):
        item.update(stage='processing', status='human_followup', title='等待顾客' if item['operations']['customer_wait']['waiting'] else '人工跟进中')
    if item['type'] == 'logistics_exception' and item['status'] != 'closed':
        item['stage'] = 'needs_info' if item['operations']['customer_wait']['waiting'] else 'processing' if item['operations']['version'] else 'pending'
    return item


def preserve_handoff_items(c, user, cid):
    """Called by service._handoff before changing waiting_human to human."""
    _staff(user)
    for item in support._work_items(c, user):
        if item['type'] != 'conversation' or item['conversation_id'] != cid: continue
        c.execute('INSERT OR IGNORE INTO work_followup_anchors VALUES (?,?,?,?)', ('conversation', item['id'], cid, _json(item)))


def _all_items(c, user):
    items = support._work_items(c, user); keys = {(i['type'], i['id']) for i in items}
    from . import logistics_operations
    items.extend(logistics_operations.queue_items(c))
    for row in c.execute('SELECT data FROM work_followup_anchors'):
        item = json.loads(row[0])
        if (item['type'], item['id']) in keys: continue
        if not c.execute('SELECT 1 FROM conversations WHERE id=? AND user_id=?', (item['conversation_id'], item['user_id'])).fetchone(): continue
        items.append({**item, 'needs_handoff': False})
        keys.add((item['type'], item['id']))
    for row in c.execute("SELECT data FROM work_operations WHERE target_type='conversation'"):
        record = json.loads(row[0]); item = record['anchor']
        if (item['type'], item['id']) in keys or record['followup_state'] == 'done': continue
        if not c.execute('SELECT 1 FROM conversations WHERE id=? AND user_id=?', (item['conversation_id'], item['user_id'])).fetchone(): continue
        items.append({**item, 'needs_handoff': False, 'updated_at': record['updated_at']})
    enriched = [_enrich(c, i) for i in items]
    return [i for i in enriched if i['type'] != 'conversation' or i['operations']['followup_state'] != 'done']


def _resolve(c, user, target):
    typ, identity = _target_key(target)
    if typ == 'logistics_exception':
        from . import logistics_operations
        return _enrich(c, logistics_operations.exception_anchor(c, identity))
    item = next((i for i in support._work_items(c, user) if i['type'] == typ and i['id'] == identity), None)
    if item is None:
        row = c.execute('SELECT data FROM work_operations WHERE target_type=? AND target_id=?', (typ, identity)).fetchone()
        if row: item = json.loads(row[0])['anchor']
        if item is None and typ == 'conversation':
            anchor = c.execute('SELECT data FROM work_followup_anchors WHERE target_type=? AND target_id=?', (typ, identity)).fetchone()
            if anchor: item = json.loads(anchor[0])
        if item is None and typ in ['case', 'service_request']:
            table = 'cases' if typ == 'case' else 'service_requests'; row = c.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
            if row:
                v = json.loads(row['data']); person = json.loads(service.one(c, 'SELECT data FROM users WHERE id=?', (row['user_id'],))[0])
                item = {'id': identity, 'type': typ, 'conversation_id': row['conversation_id'], 'user_id': row['user_id'], 'user_name': person['name'], 'order_id': v.get('order_id'), 'status': v['status'], 'stage': support.STAGES.get(v['status'], 'closed'), 'title': '已结束事项', 'reason': v.get('reason', ''), 'request_type': v.get('type', v.get('kind')), 'intent_ids': support._linked_intent_ids(v), 'source_message_id': v.get('source_message_id')}
    if not item: _fail(404, '待办不存在或已发生变化，请刷新列表')
    conv = service._conversation(c, user, item['conversation_id'])
    if conv['user_id'] != item['user_id']: _fail(409, '待办归属已变化，请刷新列表')
    if typ in ['case', 'service_request']:
        table = 'cases' if typ == 'case' else 'service_requests'; row = service.one(c, f'SELECT * FROM {table} WHERE id=?', (identity,), '原业务记录已不存在')
        v = json.loads(row['data'])
        if row['user_id'] != item['user_id'] or row['conversation_id'] != item['conversation_id']: _fail(409, '待办归属已变化，请刷新列表')
        item = {**item, 'status': v['status'], 'stage': support.STAGES.get(v['status'], 'closed'), 'order_id': v.get('order_id')}
    item = dict(item)
    if typ == 'conversation':
        linked = [i for i in support._intents(c, conv['id']) if i.get('id') in item.get('intent_ids', [])]
        item['source_message_ids'] = list(dict.fromkeys(m for i in linked for m in (i.get('source_message_ids') or [i.get('source_message_id')]) if m))
        item['needs_handoff'] = conv['mode'] == 'waiting_human'
    return _enrich(c, item)


def _open(item):
    return (item['operations']['followup_state'] != 'done') and (item['type'] == 'conversation' or item['type'] == 'logistics_exception' and item['status'] != 'closed' or item['status'] in support.STAGES)


def _editable(item):
    if not _open(item): _fail(409, '该事项已结束，只能查看历史，不能再修改或发送处理卡')


def _summary(item):
    return {**{k: item.get(k) for k in ['type', 'id', 'title', 'user_name', 'conversation_id', 'order_id', 'request_type', 'status']}, 'can_edit': _open(item)}


def _deadline(item, scope):
    ops = item['operations']
    return ops['staff_due_at'] if scope == 'staff' else (ops.get('platform_deadline') or {}).get('due_at')


def work_items(user, stage='all', conversation_id=None, assignee='all', deadline_scope='staff', deadline='all', q=''):
    _staff(user)
    if stage not in ['all', *support.STAGES.values(), 'waiting_human']: _fail(422, '不支持的待办阶段')
    if deadline_scope not in ['staff', 'platform'] or deadline not in ['all', 'overdue', 'today', 'unset']: _fail(422, '请选择有效的截止时间筛选')
    q = _text(q, '搜索内容', 200, False); as_of = db.now(); at = datetime.fromisoformat(as_of); today = at.astimezone(ZoneInfo('Asia/Shanghai')).date()
    with _read() as c:
        core = support._work_items(c, user); items = _all_items(c, user)
        mine = {r['id'] for r in c.execute('SELECT id FROM work_operators WHERE linked_user_id=?', (user['id'],))}
        if assignee not in ['all', 'mine', 'unassigned']: _operator(c, assignee)
        selected = []
        for item in items:
            if conversation_id and item['conversation_id'] != conversation_id: continue
            if stage != 'all' and not (item.get('needs_handoff') if stage == 'waiting_human' else item['stage'] == stage): continue
            aid = (item['operations'].get('assignee') or {}).get('id')
            if assignee == 'mine' and aid not in mine: continue
            if assignee == 'unassigned' and aid is not None: continue
            if assignee not in ['all', 'mine', 'unassigned'] and aid != assignee: continue
            due = _deadline(item, deadline_scope); dt = datetime.fromisoformat(due) if due else None
            if deadline == 'unset' and due: continue
            if deadline == 'overdue' and (dt is None or dt >= at): continue
            if deadline == 'today' and (dt is None or dt.astimezone(ZoneInfo('Asia/Shanghai')).date() != today): continue
            hay = ' '.join(str(item.get(k) or '') for k in ['user_name', 'title', 'reason', 'order_id']) + ' ' + item['operations']['next_step']
            if q and q.casefold() not in hay.casefold(): continue
            selected.append(item)
        counts = {key: sum(i['stage'] == key for i in core) for key in (*support.STAGES.values(), 'waiting_human')}; counts['waiting_human'] = sum(bool(i.get('needs_handoff')) for i in core)
        queue_counts = {key: sum(i['stage'] == key for i in items) for key in (*support.STAGES.values(), 'waiting_human')}
        queue_counts['waiting_human'] = sum(bool(i.get('needs_handoff')) for i in items)
        opcounts = {'unassigned': sum(not i['operations']['assignee'] for i in items), 'waiting_customer': sum(i['operations']['customer_wait']['waiting'] for i in items), 'staff_deadline_unset': sum(not _deadline(i, 'staff') for i in items), 'platform_deadline_unset': sum(not _deadline(i, 'platform') for i in items)}
        return {'items': selected, 'total': len(items), 'filtered_total': len(selected), 'stage_counts': counts, 'queue_stage_counts': queue_counts, 'operations_counts': opcounts, 'as_of': as_of, 'filter_scope': {'stage_counts': '原业务待办阶段，未加入内部等待或跟进状态', 'queue_stage_counts': '全部当前待办，含接管后继续跟进事项；未应用筛选', 'operations_counts': '全部当前待办，未应用筛选', 'deadline_scope': deadline_scope, 'deadline': deadline, 'timezone': 'Asia/Shanghai', 'unset': '未设置单列，不算准时；等待顾客不暂停期限'}}


def get_operations(user, type, id):
    _staff(user)
    with _read() as c:
        item = _resolve(c, user, {'type': type, 'id': id})
        return {'item': item, 'operations': item['operations']}


def _changes(c, user, changes):
    _keys(changes, ['assignee_id', 'next_step', 'staff_due_at', 'platform_deadline', 'customer_wait', 'followup_state'])
    if not changes: _fail(422, '请填写需要保存的跟进内容')
    result = {}
    if 'assignee_id' in changes: result['assignee'] = None if changes['assignee_id'] is None else _operator(c, changes['assignee_id'], True)
    if 'next_step' in changes: result['next_step'] = _text(changes['next_step'], '下一步', 500, False)
    if 'staff_due_at' in changes: result['staff_due_at'] = _time(changes['staff_due_at'], '员工跟进截止时间')
    if 'platform_deadline' in changes:
        v = changes['platform_deadline']
        if v is None: result['platform_deadline'] = None
        else:
            _keys(v, ['due_at', 'source_note']); due = _time(v.get('due_at'), '平台截止时间')
            if not due: _fail(422, '请选择平台截止时间，或明确清空')
            result['platform_deadline'] = {'due_at': due, 'source_kind': 'manual', 'source_label': '人工登记', 'source_note': _text(v.get('source_note'), '平台期限来源说明'), 'collected_at': db.now(), 'recorded_by': _actor(user)}
    if 'customer_wait' in changes:
        v = changes['customer_wait']; _keys(v, ['waiting', 'reason'])
        if type(v.get('waiting')) is not bool: _fail(422, '请选择是否等待顾客')
        result['customer_wait'] = {'waiting': v['waiting'], 'reason': _text(v.get('reason', ''), '等待说明', 500, v['waiting']), 'since': db.now() if v['waiting'] else None, 'actor': _actor(user)}
    if 'followup_state' in changes:
        if changes['followup_state'] not in ['open', 'done']: _fail(422, '请选择有效的内部跟进状态')
        result['followup_state'] = changes['followup_state']
    return result


def _save_operations(c, user, item, changes, reason, action='save', registration=None):
    ops = item['operations']; before = {k: ops.get(k) for k in ['assignee', 'next_step', 'staff_due_at', 'platform_deadline', 'customer_wait', 'followup_state']}
    if changes.get('customer_wait', {}).get('waiting') and ops['customer_wait']['waiting']:
        changes['customer_wait']['since'] = ops['customer_wait']['since']
    value = {k: v for k, v in ops.items() if k != 'history'}; value.update(changes)
    value.update(version=ops['version'] + 1, updated_at=db.now())
    if registration: value['registrations'] = [*value.get('registrations', []), registration]
    value['anchor'] = {k: v for k, v in item.items() if k != 'operations'}
    before_id = (before.get('assignee') or {}).get('id'); after_id = (value.get('assignee') or {}).get('id')
    if before_id != after_id and ops['version'] > 0 and not reason: _fail(422, '转派或清空负责人需要填写原因')
    label = TITLES.get(action, '建立责任登记' if ops['version'] == 0 else '转派负责人' if before_id != after_id else '更新跟进信息')
    entry = {'id': db.uid('work_history'), 'action': action, 'action_label': label, 'reason': reason, 'actor': _actor(user), 'at': value['updated_at'], 'before': before, 'after': {k: value[k] for k in before}}
    if registration: entry['registration'] = registration
    c.execute('INSERT INTO work_operations VALUES (?,?,?,?,?) ON CONFLICT(target_type,target_id) DO UPDATE SET version=excluded.version,data=excluded.data', (item['type'], item['id'], item['conversation_id'], value['version'], _json(value)))
    c.execute('INSERT INTO work_operation_history VALUES (?,?,?,?)', (entry['id'], item['type'], item['id'], _json(entry)))
    service.audit(c, user, label, item['id'], before, entry['after'])
    return entry


def patch_operations(user, data):
    _staff(user); _keys(data, ['target', 'expected_version', 'request_id', 'changes', 'reason'])
    with db.transaction() as c:
        rid, digest, old = _memo(c, user, 'patch_operations', data)
        if old: return old
        item = _resolve(c, user, data.get('target')); _editable(item)
        if _version(data.get('expected_version')) != item['operations']['version']: _fail(409, '跟进信息已更新，请刷新后重新保存')
        if item['type'] != 'conversation' and 'followup_state' in (data.get('changes') or {}): _fail(422, '正式业务事项请通过原办理入口处理，不能用内部跟进结束代替')
        reason = _text(data.get('reason', ''), '变更原因', 500, False); changes = _changes(c, user, data.get('changes'))
        if any(k in changes for k in ['staff_due_at', 'platform_deadline']) and item['operations']['version'] > 0 and not reason: _fail(422, '调整截止时间需要填写原因')
        _save_operations(c, user, item, changes, reason)
        current = _resolve(c, user, data['target']); result = {'item': current, 'operations': current['operations']}
        return _remember(c, user, 'patch_operations', rid, digest, result)


def _context(c, item):
    orders = [json.loads(r['data']) for r in c.execute('SELECT data FROM orders WHERE user_id=?', (item['user_id'],))]
    if item.get('order_id'): orders = [o for o in orders if o['id'] == item['order_id']]
    messages = [service._message(r) for r in c.execute("SELECT * FROM messages WHERE conversation_id=? AND role='user' AND status='completed' ORDER BY rowid", (item['conversation_id'],))]
    attachments = []
    for m in messages:
        for aid in m['attachments']:
            row = c.execute('SELECT * FROM attachments WHERE id=? AND user_id=?', (aid, item['user_id'])).fetchone()
            if row: attachments.append({'id': aid, 'name': row['name'], 'message_id': m['id']})
    return orders, messages, attachments


def action_templates(user, type, id):
    _staff(user)
    with _read() as c:
        item = _resolve(c, user, {'type': type, 'id': id})
        if item['type'] == 'logistics_exception':
            return {'items': [], 'target_summary': _summary(item), 'available_orders': [], 'source_messages': [], 'existing_attachments': [], 'notice': '内部物流跟进没有顾客办理授权，请在物流入口核对、关联或关闭异常。'}
        orders, messages, attachments = _context(c, item)
        definitions = {'register_shortage': [('order_id','order'),('missing_item','text'),('missing_quantity','number'),('source_kind','source'),('source_message_id','message')], 'wait_photos': [('photo_requirements','textarea'),('materials_reviewed','checkbox')], 'transfer_after_sale': []}
        cards = []
        for aid, fields in definitions.items():
            fields = [('assignee_id','operator'),('next_step','textarea'),('reason','textarea')] + fields
            cards.append({'id': aid, 'title': TITLES[aid], 'description': {'register_shortage': '登记待核实陈述，不代表已确认少发或安排补发。', 'wait_photos': '核对已有材料后明确补充内容，只改变内部等待状态。', 'transfer_after_sale': '登记售后跟进责任，不代表已审核、退款或出库。'}[aid], 'input_fields': [{'field': f, 'label': FIELDS[f], 'type': t, 'required': f != 'source_message_id'} for f,t in fields], 'disabled_reason': None if _open(item) else '该事项已结束，只能查看历史'})
        return {'items': cards, 'target_summary': _summary(item), 'available_orders': [{k:o.get(k) for k in ['id','product_name','sku_label','quantity']} for o in orders], 'source_messages': [{k:m[k] for k in ['id','content','attachments','created_at']} for m in messages], 'existing_attachments': attachments}


def _inputs(c, user, item, action, inputs):
    _keys(inputs, FIELDS); missing = []
    def need(field, value=True):
        if not value: missing.append({'field':field,'label':FIELDS[field],'message':'请填写或确认'+FIELDS[field]})
    need('assignee_id', inputs.get('assignee_id')); need('next_step', isinstance(inputs.get('next_step'),str) and inputs['next_step'].strip()); need('reason', isinstance(inputs.get('reason'),str) and inputs['reason'].strip())
    if action == 'register_shortage':
        for key in ['order_id','missing_item','source_kind']: need(key,inputs.get(key))
        need('missing_quantity',type(inputs.get('missing_quantity')) is int and 1<=inputs['missing_quantity']<=9999)
        if inputs.get('source_kind') == 'customer':
            need('source_message_id',inputs.get('source_message_id'));need('source_quote',inputs.get('source_quote'))
        elif inputs.get('source_kind') not in [None,'','staff']: _fail(422,'请选择顾客反映或员工登记作为陈述来源')
    elif action == 'wait_photos':
        need('photo_requirements',inputs.get('photo_requirements'));need('materials_reviewed',inputs.get('materials_reviewed') is True)
    if missing: return None,missing
    v={'assignee_id':_operator(c,inputs['assignee_id'],True)['id'],'next_step':_text(inputs['next_step'],'下一步'),'reason':_text(inputs['reason'],'跟进原因')}
    orders,messages,attachments=_context(c,item); allowed={o['id']:o for o in orders}
    oid=inputs.get('order_id') or item.get('order_id')
    if oid and oid not in allowed: _fail(422,'关联订单与当前顾客或待办不一致，请重新选择')
    v['order_id']=oid
    evidence=inputs.get('evidence_attachment_ids',[])
    if not isinstance(evidence,list) or any(not isinstance(a,str) or a not in {a['id'] for a in attachments} for a in evidence): _fail(422,'所选附件不属于当前顾客的这段会话')
    v['evidence_attachment_ids']=sorted(set(evidence))
    if action == 'register_shortage':
        v.update(missing_item=_text(inputs['missing_item'],'缺少的物品',100),missing_quantity=inputs['missing_quantity'],source_kind=inputs['source_kind'])
        if v['source_kind']=='customer':
            m=next((m for m in messages if m['id']==inputs['source_message_id']),None);quote=_text(inputs['source_quote'],'引用原文',2000)
            if not m or quote not in m['content']: _fail(422,'引用原文与本会话的顾客消息不一致')
            bound=set(item.get('source_message_ids',[]))|{item.get('source_message_id')}
            if oid not in m['content'] and m['id'] not in bound: _fail(422,'这条顾客原话尚不能明确关联所选订单，请使用员工登记待核实')
            other=[r['id'] for r in c.execute('SELECT id FROM orders WHERE user_id=?',(item['user_id'],)) if r['id']!=oid and r['id'] in quote]
            if other: _fail(422,'引用原文涉及其他订单，请重新核对来源')
            clauses=[part for part in re.split(r'[，。；\n]',quote) if v['missing_item'] in part and re.search(r'少|漏|没收到|未收到|缺',part)]
            uncertain=r'不是|没有少|没少|并未少|没有缺|不缺|没有漏|没漏|不[是为]?少|没[有]?漏|是否|是不是|[?？吗]|如果|假如|可能|好像|不确定'
            clauses=[part for part in clauses if not re.search(uncertain,part)]
            if not clauses: _fail(422,'原话未明确支持所登记的缺少物品，请使用员工登记待核实')
            nums={'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
            count=r'(\d+|[一二两三四五六七八九十])\s*(?:个|件|根|只|套|包|条)'
            name=re.escape(v['missing_item']);marker=r'(?:少(?:发|了|收到)?|漏(?:发|了)?|缺(?:少|了)?|未收到|没收到)'
            quantities=[]
            for part in clauses:
                for pattern in [marker+r'\s*'+count+r'\s*'+name,name+r'\s*'+marker+r'\s*'+count,marker+r'\s*'+name+r'\s*'+count,count+r'\s*'+name+r'\s*(?:未收到|没收到|缺失)']:
                    quantities.extend(int(x) if x.isdecimal() else nums[x] for x in re.findall(pattern,part))
            if set(quantities)!={v['missing_quantity']}: _fail(422,'原话未明确支持所登记物品的缺少数量，请使用员工登记待核实')
            v.update(source_message_id=m['id'],source_quote=quote)
        else: v.update(source_message_id=None,source_quote=None)
    if action=='wait_photos':
        photos=_text(inputs['photo_requirements'],'需要补充的照片',500)
        if re.search(r'(?:已|已经|保证|承诺|确定).{0,8}(?:退款|补发|发货|出库|批准|赔付)|无需.{0,4}(?:审核|确认)',photos): _fail(422,'照片说明请只填写需要补充的材料，不要填写处理完成或办理承诺')
        v.update(photo_requirements=photos,materials_reviewed=True)
    return v,[]


def _fingerprint(c,item,inputs):
    conv=dict(service.one(c,'SELECT * FROM conversations WHERE id=?',(item['conversation_id'],)))
    messages=[dict(r) for r in c.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid',(item['conversation_id'],))]
    business=None
    if item['type'] in ['case','service_request']:
        table='cases' if item['type']=='case' else 'service_requests';business=dict(service.one(c,f'SELECT * FROM {table} WHERE id=?',(item['id'],)))
    order=json.loads(service.one(c,'SELECT data FROM orders WHERE id=?',(inputs['order_id'],))[0]) if inputs.get('order_id') else None
    evidence_ids=set(inputs.get('evidence_attachment_ids',[]))
    for message in messages: evidence_ids.update(json.loads(message['attachments']))
    evidence=[dict(service.one(c,'SELECT * FROM attachments WHERE id=?',(aid,))) for aid in sorted(evidence_ids)]
    return _hash({'target':{k:item.get(k) for k in ['type','id','conversation_id','user_id','order_id','intent_ids','source_message_id']},'operations_version':item['operations']['version'],'business':business,'conversation':conv,'messages':messages,'intents':support._intents(c,item['conversation_id']),'order':order,'evidence':evidence,'assignee':_operator(c,inputs['assignee_id']),'inputs':inputs,'template_revision':1})


def preview_action(user,data):
    _staff(user);_keys(data,['target','action_id','expected_version','inputs'])
    action=data.get('action_id')
    if action not in TITLES:_fail(422,'请选择有效的中文处理卡')
    with db.transaction() as c:
        item=_resolve(c,user,data.get('target'));_editable(item)
        if item['type']=='logistics_exception':_fail(422,'内部物流异常不能发送顾客处理卡，请使用物流跟进入口')
        if _version(data.get('expected_version'))!=item['operations']['version']:_fail(409,'跟进信息已更新，请刷新后重新预览')
        inputs,missing=_inputs(c,user,item,action,data.get('inputs'))
        expires=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        result={'can_confirm':not missing,'missing_fields':missing,'effects':[],'customer_message':'','target_summary':_summary(item),'expires_at':expires,'expected_version':item['operations']['version']}
        if missing:return result
        prefix=''
        if inputs.get('order_id'):
            order=json.loads(service.one(c,'SELECT data FROM orders WHERE id=?',(inputs['order_id'],))[0])
            prefix=f'关于订单 {order["id"]}（{order["product_name"]}）：'
        if action=='register_shortage':
            lead='已记录您反馈的商品数量问题。' if inputs['source_kind']=='customer' else '已登记一项商品数量待核实事项。'
            reply=lead+f'登记内容为缺少“{inputs["missing_item"]}”{inputs["missing_quantity"]}件，目前仍待核实。'+'我会核对订单和装箱情况，再向您说明下一步；目前尚未确定补发安排。'
        elif action=='wait_photos':reply='为核对这项问题，还需要您补充：'+inputs['photo_requirements']+'。现有资料会保留，我会继续跟进。'
        else:reply='已登记为售后跟进事项，下一步由指定负责人核对相关情况。这一步尚未提交或执行具体售后方案，如需要确认，会再请您核对。'
        result.update(preview_id=db.uid('work_preview'),fingerprint=_fingerprint(c,item,inputs),customer_message=prefix+reply,effects=['保存跟进负责人和下一步，并追加操作历史。','接管为人工服务，取消尚未完成的智能回复；接待模式改变时会另有一条系统接入提示。','向顾客发送下方完整内容，员工回复恰好一条。','不提交售后申请，不修改审核、顾客确认、库存、退款或出库记录。'],planned_operations={'assignee':_operator(c,inputs['assignee_id']), 'next_step':inputs['next_step'], 'reason':inputs['reason'], 'customer_wait':{'waiting':True,'reason':inputs['reason']} if action=='wait_photos' else item['operations']['customer_wait']},registration={'label':TITLES[action],'source_label':'顾客反映，待核实' if inputs.get('source_kind')=='customer' else '员工登记，待核实','inputs':inputs})
        if action=='wait_photos':result['effects'].append('标记内部等待顾客；员工与平台截止时间均不暂停或变更。')
        stored={**result,'target':data['target'],'action_id':action,'inputs':inputs,'actor_id':user['id']}
        c.execute('INSERT INTO work_action_previews VALUES (?,?,?,?,?)',(result['preview_id'],user['id'],expires,0,_json(stored)))
        return result


def confirm_action(user,data):
    _staff(user);_keys(data,['preview_id','fingerprint','expected_version','request_id'])
    with db.transaction() as c:
        rid,digest,old=_memo(c,user,'confirm_action',data)
        if old:return old
        row=c.execute('SELECT * FROM work_action_previews WHERE id=? AND actor_id=?',(data.get('preview_id'),user['id'])).fetchone()
        if not row:_fail(404,'预览不存在或不属于当前员工，请重新预览')
        if row['consumed']:_fail(409,'这张处理卡已确认，不能重复发送')
        if datetime.fromisoformat(row['expires_at'])<=datetime.now(timezone.utc):_fail(409,'预览已过期，请重新预览')
        v=json.loads(row['data']);item=_resolve(c,user,v['target']);_editable(item)
        if data.get('fingerprint')!=v['fingerprint'] or _version(data.get('expected_version'))!=v['expected_version'] or item['operations']['version']!=v['expected_version'] or _fingerprint(c,item,v['inputs'])!=v['fingerprint']:_fail(409,'资料或接待状态已更新，请重新预览')
        inputs=v['inputs'];_operator(c,inputs['assignee_id'],True)
        changes={'assignee_id':inputs['assignee_id'],'next_step':inputs['next_step']}
        if v['action_id']=='wait_photos':changes['customer_wait']={'waiting':True,'reason':inputs['reason']}
        registration={'kind':v['action_id'],'label':TITLES[v['action_id']],'status':'待核实' if v['action_id']=='register_shortage' else '已登记跟进','source_label':'顾客反映，待核实' if inputs.get('source_kind')=='customer' else '员工登记，待核实','inputs':inputs,'actor':_actor(user),'at':db.now()}
        entry=_save_operations(c,user,item,_changes(c,user,changes),inputs['reason'],v['action_id'],registration)
        service._handoff(c,user,item['conversation_id'])
        message=service._add_message(c,item['conversation_id'],'staff',v['customer_message'],client_message_id='work-card:'+v['preview_id'])
        c.execute('UPDATE work_action_previews SET consumed=1 WHERE id=?',(v['preview_id'],))
        current=_resolve(c,user,v['target']);result={'item':current,'operations':current['operations'],'message':message,'history_entry':entry,'preview_id':v['preview_id']}
        return _remember(c,user,'confirm_action',rid,digest,result)
