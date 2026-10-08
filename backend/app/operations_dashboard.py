"""Explicit staff observation ledgers and reproducible local operations snapshots."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from . import db, service, support, work_operations as work

SCHEMA = '''
CREATE TABLE IF NOT EXISTS operations_repeat_contacts(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,active INTEGER NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL,UNIQUE(message_id,target_type,target_id));
CREATE TABLE IF NOT EXISTS operations_quality_reviews(id TEXT PRIMARY KEY,message_id TEXT NOT NULL UNIQUE,version INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations_work_time(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,active INTEGER NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations_ledger_history(id TEXT PRIMARY KEY,ledger TEXT NOT NULL,record_id TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations_metric_snapshots(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,expires_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS operations_work_time_group ON operations_work_time(group_id);
CREATE INDEX IF NOT EXISTS operations_ledger_record ON operations_ledger_history(ledger,record_id);
'''
TAGS = {'factual_error': '事实错误', 'policy_error': '政策错误', 'missed_intent': '漏诉求', 'repeated_question': '客服重复追问', 'poor_empathy': '共情不足', 'unclear_next_step': '下一步不清楚', 'wrong_promise': '错误承诺'}
OUTCOMES = {'no_error': '无错误', 'error': '有错误', 'unknown': '无法判断'}
_json, _hash, _text, _fail = work._json, work._hash, work._text, work._fail
TZ = ZoneInfo('Asia/Shanghai')
VERSION = '本地经营观察-1'


def init_schema(c):
    for statement in SCHEMA.split(';'):
        if statement.strip(): c.execute(statement)


def _dt(value): return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
def _load(c, table, identity, label):
    if not isinstance(identity, str): _fail(422, f'请选择有效的{label}')
    r = c.execute(f'SELECT data FROM {table} WHERE id=?', (identity,)).fetchone()
    if not r: _fail(404, f'{label}不存在，请刷新后重试')
    return json.loads(r[0])

def _target(v): return {'type': v['type'], 'id': v['id']}
def _anchor_snapshot(item, source_at=None):
    assignee = item['operations']['assignee']
    if source_at:
        history = [h for h in item['operations']['history'] if _dt(h['at']) <= _dt(source_at)]
        assignee = max(history, key=lambda h: h['at'])['after'].get('assignee') if history else None
    return {k: item.get(k) for k in ['type', 'id', 'title', 'user_id', 'user_name', 'order_id', 'conversation_id', 'source_message_id', 'source_message_ids', 'intent_ids', 'source_label', 'source_kind', 'reason', 'updated_at']} | {'assignee': assignee, 'responsibility_snapshot_at': source_at or db.now()}


def _message(c, identity, roles):
    if not isinstance(identity, str): _fail(422, '请选择真实消息')
    r = c.execute('SELECT rowid AS row_number,* FROM messages WHERE id=?', (identity,)).fetchone()
    if not r or r['role'] not in roles or r['status'] != 'completed': _fail(422, '所选消息不是有效的顾客原话' if roles == ['user'] else '所选消息不是已完成的真实客服回复')
    v = service._message(r); conv = dict(c.execute('SELECT * FROM conversations WHERE id=?', (r['conversation_id'],)).fetchone())
    v['user_id'] = conv['user_id']; return v, conv


def _origin(c, item):
    source_ids = set(item.get('source_message_ids') or []) | {item.get('source_message_id')}
    source_ids.discard(None); record = None
    if item['type'] in ['case', 'service_request']:
        record = _load(c, 'cases' if item['type'] == 'case' else 'service_requests', item['id'], '原事项')
        source_ids.update(record.get('source_message_ids') or [])
        if record.get('source_message_id'): source_ids.add(record['source_message_id'])
    sources = []
    for identity in source_ids:
        row = c.execute('SELECT * FROM messages WHERE id=? AND role=\'user\'', (identity,)).fetchone()
        if row: sources.append(service._message(row))
    at = min([s['created_at'] for s in sources] + ([record['created_at']] if record and record.get('created_at') else []), default=None)
    return {'source_message_ids': sorted(source_ids), 'source_messages': sorted(sources, key=lambda x: x['created_at']), 'created_at': at, 'record_reason': record.get('reason') if record else item.get('reason'), 'origin_type': '顾客业务记录' if record else '原会话诉求'}


def _validate_repeat(c, user, message_id, target, order_id):
    message, conv = _message(c, message_id, ['user']); item = work._resolve(c, user, target)
    if item['type'] == 'logistics_exception': _fail(422, '员工或规则发现的物流异常不能作为顾客原咨询事项；请选择真实顾客事项')
    if item['user_id'] != message['user_id']: _fail(422, '顾客消息与原事项不是同一顾客，不能关联')
    if order_id != item.get('order_id'): _fail(422, '请明确核对本次消息与原事项的关联订单')
    if item.get('order_id'):
        row = c.execute('SELECT user_id FROM orders WHERE id=?', (order_id,)).fetchone()
        if not row or row[0] != message['user_id']: _fail(422, '关联订单不属于这位顾客')
        other_orders = [r[0] for r in c.execute('SELECT id FROM orders WHERE user_id=?', (message['user_id'],)) if r[0] != order_id and r[0] in message['content']]
        if other_orders or conv.get('selected_order_id') and conv['selected_order_id'] != order_id and order_id not in message['content']: _fail(422, '本次消息涉及其他订单，请核对具体事项后再关联')
    origin = _origin(c, item)
    if message_id in origin['source_message_ids']: _fail(422, '这条消息就是原事项来源，不能登记为再次咨询')
    if not origin['created_at']: _fail(422, '原事项缺少最早来源时间，请先核对原始记录')
    if _dt(message['created_at']) <= _dt(origin['created_at']): _fail(422, '本次顾客消息没有晚于原事项来源，不能登记为再次咨询')
    return message, item, origin


def _history(c, user, ledger, identity, before, after, reason):
    row = {'id': db.uid('observation_history'), 'ledger': ledger, 'record_id': identity, 'before': before, 'after': after, 'reason': reason, 'actor': work._actor(user), 'at': db.now()}
    c.execute('INSERT INTO operations_ledger_history VALUES (?,?,?,?)', (row['id'], ledger, identity, _json(row)))
    service.audit(c, user, {'repeat': '登记或更正再次咨询', 'quality': '登记或更正回复抽查', 'time': '登记或更正人工耗时'}[ledger], identity, before, after)


def _histories(c, ledger, identity):
    return [json.loads(r[0]) for r in c.execute('SELECT data FROM operations_ledger_history WHERE ledger=? AND record_id=? ORDER BY rowid', (ledger, identity))]


def create_repeat(user, data):
    service.require_staff(user); work._keys(data, ['message_id', 'target', 'order_id', 'reason', 'request_id'])
    with db.transaction() as c:
        rid, digest, old = work._memo(c, user, 'observations:repeat', data)
        if old: return old
        message, item, origin = _validate_repeat(c, user, data.get('message_id'), data.get('target'), data.get('order_id'))
        reason = _text(data.get('reason'), '明确关联的原因'); existing = c.execute('SELECT data FROM operations_repeat_contacts WHERE message_id=? AND target_type=? AND target_id=?', (message['id'], item['type'], item['id'])).fetchone()
        if existing:
            value = json.loads(existing[0])
            if not value['active']: _fail(409, '这条关联曾撤销，请打开原记录并填写原因恢复，不能新建重复记录')
            return work._remember(c, user, 'observations:repeat', rid, digest, value)
        v = {'id': db.uid('repeat_contact'), 'message_id': message['id'], 'target': _target(item), 'target_snapshot': _anchor_snapshot(item, message['created_at']), 'origin_snapshot': origin, 'message_snapshot': message, 'user_id': item['user_id'], 'order_id': item.get('order_id'), 'conversation_id': message['conversation_id'], 'actor': work._actor(user), 'reason': reason, 'active': True, 'version': 1, 'created_at': db.now(), 'updated_at': db.now()}
        c.execute('INSERT INTO operations_repeat_contacts VALUES (?,?,?,?,?,?,?)', (v['id'], message['id'], item['type'], item['id'], 1, 1, _json(v)))
        _history(c, user, 'repeat', v['id'], None, v, reason)
        return work._remember(c, user, 'observations:repeat', rid, digest, v)


def patch_repeat(user, identity, data):
    service.require_staff(user); work._keys(data, ['expected_version', 'request_id', 'reason', 'active', 'target', 'order_id'])
    with db.transaction() as c:
        rid, digest, old = work._memo(c, user, 'observations:repeat:' + identity, data)
        if old: return old
        v = _load(c, 'operations_repeat_contacts', identity, '再次咨询记录'); before = dict(v)
        if work._version(data.get('expected_version')) != v['version']: _fail(409, '再次咨询记录已更新，请刷新后重试')
        reason = _text(data.get('reason'), '更正或撤销原因')
        if 'active' in data:
            if type(data['active']) is not bool: _fail(422, '请选择关联有效或撤销')
            v['active'] = data['active']
        if 'target' in data:
            message, item, origin = _validate_repeat(c, user, v['message_id'], data['target'], data.get('order_id'))
            exists = c.execute('SELECT id FROM operations_repeat_contacts WHERE message_id=? AND target_type=? AND target_id=? AND id!=?', (v['message_id'], item['type'], item['id'], identity)).fetchone()
            if exists: _fail(409, '此消息与该事项的关联已存在，请使用已有记录')
            v.update(target=_target(item), target_snapshot=_anchor_snapshot(item, message['created_at']), origin_snapshot=origin, order_id=item.get('order_id'))
        v.update(version=v['version'] + 1, updated_at=db.now(), reason=reason)
        c.execute('UPDATE operations_repeat_contacts SET target_type=?,target_id=?,active=?,version=?,data=? WHERE id=?', (v['target']['type'], v['target']['id'], int(v['active']), v['version'], _json(v), identity))
        _history(c, user, 'repeat', identity, before, v, reason)
        return work._remember(c, user, 'observations:repeat:' + identity, rid, digest, v)


def _quality_values(data):
    outcome = data.get('outcome'); tags = data.get('tags', [])
    if outcome not in OUTCOMES: _fail(422, '请明确选择无错误、有错误或无法判断')
    if not isinstance(tags, list) or any(not isinstance(t, str) or t not in support.QUALITY_TAGS for t in tags): _fail(422, '请选择有效的服务错误标签')
    if outcome == 'error' and not tags: _fail(422, '有错误的抽查至少选择一个有效错误标签')
    if outcome != 'error' and tags: _fail(422, '无错误或无法判断的样本请清空错误标签')
    return outcome, sorted(set(tags)), _text(data.get('note'), '抽查说明', 2000), _text(data.get('evidence_note'), '当时证据或无法判断的依据说明', 3000)


def _review_evidence(c, message, item, note):
    prior = [service._message(r) for r in c.execute("SELECT * FROM messages WHERE conversation_id=? AND rowid<? AND status='completed' ORDER BY rowid DESC LIMIT 40", (message['conversation_id'], message['row_number']))]
    referenced = None
    if item and item['type'] in ['case', 'service_request']:
        referenced = _load(c, 'cases' if item['type'] == 'case' else 'service_requests', item['id'], '所属事项')
    policies = [json.loads(r[0]) for r in c.execute('SELECT data FROM policies')]
    before = [p for p in policies if p.get('published_at') and p['published_at'] <= message['created_at']]
    policy = max(before, key=lambda p: p['version']) if before else None
    return {'reply_snapshot': {k: message.get(k) for k in ['id', 'role', 'content', 'cards', 'attachments', 'created_at', 'turn_id']}, 'preceding_messages': list(reversed(prior)), 'historical_policy_snapshot': policy, 'record_at_review_snapshot': referenced, 'evidence_note': note, 'recorded_at': db.now(), 'evidence_scope': '回复原文、消息内资料及回复前原话保留原始快照；业务记录为抽查登记时核对值，未保存的回复时资料保持未知。'}


def _legacy_quality(c, user, item, tags, note, review_id):
    if not tags or not item or item['type'] not in ['case', 'service_request']: return None
    # Same validation as support.record_quality; same caller-owned transaction.
    if any(t not in support.QUALITY_TAGS for t in tags): _fail(422, '请选择有效的服务错误标签')
    v = {'id': db.uid('quality'), 'actor_id': user['id'], 'target_type': item['type'], 'target_id': item['id'], 'tags': tags, 'note': note, 'created_at': db.now(), 'review_id': review_id}
    c.execute('INSERT INTO service_quality VALUES (?,?,?,?,?,?)', (v['id'], user['id'], item['type'], item['id'], _json(v), v['created_at']))
    return v['id']


def create_quality(user, data):
    service.require_staff(user); work._keys(data, ['message_id', 'target', 'outcome', 'tags', 'note', 'evidence_note', 'request_id'])
    with db.transaction() as c:
        rid, digest, old = work._memo(c, user, 'observations:quality', data)
        if old: return old
        message, conv = _message(c, data.get('message_id'), ['assistant', 'staff'])
        if c.execute('SELECT 1 FROM operations_quality_reviews WHERE message_id=?', (message['id'],)).fetchone(): _fail(409, '这条回复已有抽查样本，请在原记录更正，不能重复扩大分母')
        item = work._resolve(c, user, data['target']) if data.get('target') else None
        if item and (item['type'] == 'logistics_exception' or item['user_id'] != conv['user_id'] or item['conversation_id'] != message['conversation_id']): _fail(422, '所属事项与这条回复的顾客或会话不一致')
        outcome, tags, note, evidence_note = _quality_values(data)
        v = {'id': db.uid('quality_review'), 'message_id': message['id'], 'message_snapshot': message, 'conversation_id': message['conversation_id'], 'user_id': conv['user_id'], 'target': _target(item) if item else None, 'target_snapshot': _anchor_snapshot(item, message['created_at']) if item else None, 'outcome': outcome, 'outcome_label': OUTCOMES[outcome], 'tags': tags, 'note': note, 'evidence_snapshot': _review_evidence(c, message, item, evidence_note), 'actor': work._actor(user), 'created_at': db.now(), 'completed_at': db.now(), 'updated_at': db.now(), 'version': 1}
        v['service_quality_id'] = _legacy_quality(c, user, item, tags, note, v['id'])
        c.execute('INSERT INTO operations_quality_reviews VALUES (?,?,?,?)', (v['id'], message['id'], 1, _json(v))); _history(c, user, 'quality', v['id'], None, v, note)
        return work._remember(c, user, 'observations:quality', rid, digest, v)


def patch_quality(user, identity, data):
    service.require_staff(user); work._keys(data, ['request_id', 'expected_version', 'outcome', 'tags', 'note', 'evidence_note', 'reason'])
    with db.transaction() as c:
        rid, digest, old = work._memo(c, user, 'observations:quality:' + identity, data)
        if old: return old
        v = _load(c, 'operations_quality_reviews', identity, '回复抽查'); before = dict(v)
        if work._version(data.get('expected_version')) != v['version']: _fail(409, '抽查结论已更新，请刷新后重试')
        reason = _text(data.get('reason'), '更正原因'); outcome, tags, note, evidence_note = _quality_values(data)
        item = work._resolve(c, user, v['target']) if v['target'] else None
        v.update(outcome=outcome, outcome_label=OUTCOMES[outcome], tags=tags, note=note, version=v['version'] + 1, updated_at=db.now(), corrected_at=db.now(), actor=work._actor(user))
        # Original reply and original supporting snapshot remain immutable in history.
        v['evidence_snapshot'] = before['evidence_snapshot'] | {'evidence_note': evidence_note, 'corrected_at': db.now()}
        v['service_quality_id'] = _legacy_quality(c, user, item, tags, note, identity)
        c.execute('UPDATE operations_quality_reviews SET version=?,data=? WHERE id=?', (v['version'], _json(v), identity)); _history(c, user, 'quality', identity, before, v, reason)
        return work._remember(c, user, 'observations:quality:' + identity, rid, digest, v)


def _work_date(value):
    if not isinstance(value, str): _fail(422, '请选择实际工作日期')
    try: day = date.fromisoformat(value)
    except ValueError: _fail(422, '工作日期格式不正确')
    if day > _dt(db.now()).astimezone(TZ).date(): _fail(422, '工作日期不能在未来')
    return day.isoformat()

def _minutes(value):
    if type(value) is not int or not 1 <= value <= 720: _fail(422, '每次投入分钟应为 1–720 的整数，超过请分次核对')
    return value


def create_time(user, data):
    service.require_staff(user); work._keys(data, ['request_id', 'work_date', 'description', 'minutes', 'target', 'total_minutes', 'allocations'])
    day = _work_date(data.get('work_date')); description = _text(data.get('description'), '实际工作内容', 2000)
    if 'allocations' in data:
        if 'target' in data or 'minutes' in data: _fail(422, '单项与分摊登记不能同时填写')
        allocations = data['allocations']; total = _minutes(data.get('total_minutes'))
        if not isinstance(allocations, list) or not 1 <= len(allocations) <= 50: _fail(422, '请选择 1–50 项真实待办分摊')
        for a in allocations: work._keys(a, ['target', 'minutes']); _minutes(a.get('minutes'))
        if sum(a['minutes'] for a in allocations) != total: _fail(422, '各事项分摊分钟数合计必须等于本次实际总投入，不能重复计时')
    else:
        total = _minutes(data.get('minutes')); allocations = [{'target': data.get('target'), 'minutes': total}]
        if 'total_minutes' in data: _fail(422, '单项登记只填写实际分钟数')
    with db.transaction() as c:
        rid, digest, old = work._memo(c, user, 'observations:time', data)
        if old: return old
        items = [work._resolve(c, user, a['target']) for a in allocations]
        if len({(i['type'], i['id']) for i in items}) != len(items): _fail(422, '同一事项请合并分钟数，不要在本次分摊中重复选择')
        group = db.uid('work_time_group'); entries = []
        for allocation, item in zip(allocations, items):
            v = {'id': db.uid('work_time'), 'group_id': group, 'declared_total_minutes': total, 'target': _target(item), 'target_snapshot': _anchor_snapshot(item, day + 'T23:59:59+08:00'), 'actor': work._actor(user), 'work_date': day, 'minutes': allocation['minutes'], 'description': description, 'method': '员工填写实际投入', 'active': True, 'created_at': db.now(), 'updated_at': db.now(), 'version': 1}
            c.execute('INSERT INTO operations_work_time VALUES (?,?,?,?,?)', (v['id'], group, 1, 1, _json(v))); _history(c, user, 'time', v['id'], None, v, '登记实际投入'); entries.append(v)
        return work._remember(c, user, 'observations:time', rid, digest, {'items': entries, 'group_id': group, 'total_minutes': total})


def patch_time(user, identity, data):
    service.require_staff(user); work._keys(data, ['request_id', 'expected_version', 'reason', 'active', 'minutes', 'work_date', 'description'])
    with db.transaction() as c:
        rid, digest, old = work._memo(c, user, 'observations:time:' + identity, data)
        if old: return old
        v = _load(c, 'operations_work_time', identity, '人工耗时'); before = dict(v)
        if work._version(data.get('expected_version')) != v['version']: _fail(409, '人工耗时记录已更新，请刷新后重试')
        reason = _text(data.get('reason'), '更正或撤销原因')
        if 'active' in data:
            if type(data['active']) is not bool: _fail(422, '请选择有效或撤销')
            v['active'] = data['active']
        if 'minutes' in data: v['minutes'] = _minutes(data['minutes'])
        if 'work_date' in data: v['work_date'] = _work_date(data['work_date'])
        if 'description' in data: v['description'] = _text(data['description'], '实际工作内容', 2000)
        v.update(version=v['version'] + 1, updated_at=db.now(), last_correction_by=work._actor(user))
        c.execute('UPDATE operations_work_time SET active=?,version=?,data=? WHERE id=?', (int(v['active']), v['version'], _json(v), identity)); _history(c, user, 'time', identity, before, v, reason)
        return work._remember(c, user, 'observations:time:' + identity, rid, digest, v)


def _time_view(c, v):
    group = [json.loads(r[0]) for r in c.execute('SELECT data FROM operations_work_time WHERE group_id=? AND active=1', (v['group_id'],))]
    return v | {'current_group_total_minutes': sum(r['minutes'] for r in group), 'group_record_count': len(group), 'history': _histories(c, 'time', v['id'])}


def observation_context(user, conversation_id=None):
    service.require_staff(user)
    with work._read() as c:
        items = work._all_items(c, user)
        # Closed business records remain selectable for follow-up audits/time.
        keys = {(i['type'], i['id']) for i in items}
        for table, typ in [('cases', 'case'), ('service_requests', 'service_request')]:
            for row in c.execute(f'SELECT id FROM {table}'):
                if (typ, row[0]) not in keys: items.append(work._resolve(c, user, {'type': typ, 'id': row[0]}))
        messages = []
        query = "SELECT messages.*,conversations.user_id FROM messages JOIN conversations ON conversations.id=messages.conversation_id WHERE messages.status='completed' AND messages.role IN ('user','assistant','staff')"
        params = ()
        if conversation_id: query += ' AND messages.conversation_id=?'; params = (conversation_id,)
        for row in c.execute(query + ' ORDER BY messages.rowid DESC LIMIT 500', params):
            m = service._message(row); person = _load(c, 'users', row['user_id'], '顾客'); m['user_name'] = person['name']; messages.append(m)
        return {'items': [_anchor_snapshot(i) | {'status': i['status'], 'stage': i['stage']} for i in items], 'messages': messages, 'message_limit': 500, 'quality_tags': TAGS, 'notice': '从真实顾客消息和客服回复显式选择；负责人是内部责任登记，操作人始终为当前登录员工。'}


def list_ledger(user, ledger):
    service.require_staff(user)
    with work._read() as c:
        table = {'repeat': 'operations_repeat_contacts', 'quality': 'operations_quality_reviews', 'time': 'operations_work_time'}.get(ledger)
        if not table: _fail(422, '请选择有效观察记录')
        rows = [json.loads(r[0]) for r in c.execute(f'SELECT data FROM {table} ORDER BY rowid DESC')]
        return {'items': [_time_view(c, v) if ledger == 'time' else v | {'history': _histories(c, ledger, v['id'])} for v in rows], 'as_of': db.now()}


def _period(start, end, as_of):
    last = _dt(as_of).astimezone(TZ).date()
    try:
        first = date.fromisoformat(start) if start else last - timedelta(days=29)
        final = date.fromisoformat(end) if end else last
    except (TypeError, ValueError): _fail(422, '请填写有效的起止日期')
    if first > final or (final - first).days > 3660: _fail(422, '开始日期不能晚于结束日期，范围最多十年')
    return first.isoformat(), final.isoformat()

def _in_period(value, start, end, is_date=False):
    if not value: return False
    day = value if is_date else _dt(value).astimezone(TZ).date().isoformat()
    return start <= day <= end

def _ratio(n, d): return {'numerator': n, 'denominator': d, 'rate': n / d if d else None}

def _assignee_filter(snapshot, assignee, mine):
    aid = (snapshot.get('assignee') or {}).get('id')
    return assignee == 'all' or assignee == 'mine' and aid in mine or assignee == 'unassigned' and aid is None or assignee not in ['all', 'mine', 'unassigned'] and aid == assignee


def _demo(snapshot): return snapshot.get('source_kind') == 'demo'


def _dedupe_open(items):
    # A durable conversation anchor whose exact intent/order has since become a
    # business record is a follow-up shell, not an additional independent claim.
    business = [i for i in items if i['type'] in ['case', 'service_request']]
    result = []
    for item in items:
        if item['type'] == 'conversation' and item.get('intent_ids'):
            if any(i['conversation_id'] == item['conversation_id'] and i.get('order_id') == item.get('order_id') and set(item['intent_ids']).issubset(set(i.get('intent_ids') or [])) and (not item.get('request_type') or i.get('request_type') == item.get('request_type')) for i in business): continue
        result.append(item)
    return list({(i['type'], i['id']): i for i in result}.values())


def metrics(user, start=None, end=None, assignee='all', include_demo=False):
    service.require_staff(user); as_of = db.now(); start, end = _period(start, end, as_of)
    if type(include_demo) is not bool: _fail(422, '请选择是否包含演示资料')
    # One write transaction obtains the read snapshot and persists its complete
    # membership. Later detail requests never rerun a query against live rows.
    with db.transaction() as c:
        if assignee not in ['all', 'mine', 'unassigned']: work._operator(c, assignee)
        mine = {r[0] for r in c.execute('SELECT id FROM work_operators WHERE linked_user_id=?', (user['id'],))}
        all_open = _dedupe_open(work._all_items(c, user))
        selected = [i for i in all_open if work._open(i) and (include_demo or not _demo(i)) and _assignee_filter({'assignee': i['operations']['assignee']}, assignee, mine)]
        open_rows = []
        overdue = []
        staff_set = platform_set = staff_late = platform_late = 0
        stages = {}
        for item in selected:
            ops = item['operations']; stage = 'waiting_customer' if ops['customer_wait']['waiting'] else item['stage']; stages[stage] = stages.get(stage, 0) + 1
            row = {'target': _target(item), 'item': item, 'assignee': ops['assignee'], 'source_label': item.get('source_label', '真实顾客事项'), 'stage': stage, 'next_step': ops['next_step'], 'updated_at': ops.get('updated_at') or item['updated_at'], 'staff_due_at': ops['staff_due_at'], 'platform_deadline': ops['platform_deadline'], 'customer_wait': ops['customer_wait'], 'deadline_history': [h for h in ops['history'] if h['before'].get('staff_due_at') != h['after'].get('staff_due_at') or h['before'].get('platform_deadline') != h['after'].get('platform_deadline')], 'as_of': as_of}
            staff_due = ops['staff_due_at']; platform_due = (ops['platform_deadline'] or {}).get('due_at')
            staff_set += bool(staff_due); platform_set += bool(platform_due)
            staff_over = bool(staff_due and _dt(staff_due) < _dt(as_of)); platform_over = bool(platform_due and _dt(platform_due) < _dt(as_of))
            staff_late += staff_over; platform_late += platform_over
            row.update(staff_overdue=staff_over, platform_overdue=platform_over, staff_overdue_minutes=max(0, (_dt(as_of) - _dt(staff_due)).total_seconds() / 60) if staff_due else None, platform_overdue_minutes=max(0, (_dt(as_of) - _dt(platform_due)).total_seconds() / 60) if platform_due else None)
            open_rows.append(row)
            if staff_over or platform_over: overdue.append(row)
        repeat_all = [json.loads(r[0]) for r in c.execute('SELECT data FROM operations_repeat_contacts')]
        repeats = [v | {'history': _histories(c, 'repeat', v['id'])} for v in repeat_all if v['active'] and _in_period(v['message_snapshot']['created_at'], start, end) and _assignee_filter(v['target_snapshot'], assignee, mine)]
        reviews_all = [json.loads(r[0]) for r in c.execute('SELECT data FROM operations_quality_reviews')]
        reviews = [v | {'history': _histories(c, 'quality', v['id'])} for v in reviews_all if _in_period(v['completed_at'], start, end) and _assignee_filter(v.get('target_snapshot') or {}, assignee, mine)]
        reviewed = {v['message_id'] for v in reviews_all}
        replies = [dict(r) for r in c.execute("SELECT messages.*,conversations.user_id FROM messages JOIN conversations ON conversations.id=messages.conversation_id WHERE role IN ('assistant','staff') AND status='completed'") if _in_period(r['created_at'], start, end)]
        # Unknown historical ownership is never projected onto today's assignee.
        unreviewed = [m for m in replies if m['id'] not in reviewed and assignee in ['all', 'unassigned']]
        errors = sum(bool(set(v['tags']) & {'factual_error', 'policy_error'}) for v in reviews)
        judgeable = sum(v['outcome'] != 'unknown' for v in reviews)
        tag_counts = {k: sum(k in v['tags'] for v in reviews) for k in TAGS}
        legacy = [json.loads(r[0]) for r in c.execute('SELECT data FROM service_quality')]
        legacy_count = sum(not v.get('review_id') for v in legacy)
        times_all = [json.loads(r[0]) for r in c.execute('SELECT data FROM operations_work_time')]
        times = [_time_view(c, v) for v in times_all if v['active'] and _in_period(v['work_date'], start, end, True) and (include_demo or not _demo(v['target_snapshot'])) and _assignee_filter(v['target_snapshot'], assignee, mine)]
        minutes = sum(v['minutes'] for v in times); timed_targets = {(v['target']['type'], v['target']['id']) for v in times}
        recorded_open = {(v['target']['type'], v['target']['id']) for v in times_all if v['active'] and _in_period(v['work_date'], start, end, True)}
        unknown_time = [r for r in open_rows if (r['target']['type'], r['target']['id']) not in recorded_open]
        details = {'unprocessed': open_rows, 'overdue': overdue, 'repeat_contacts': repeats, 'quality_reviews': reviews, 'work_time': times, 'work_time_unknown': unknown_time, 'quality_unreviewed': [{k: m[k] for k in ['id', 'conversation_id', 'role', 'content', 'created_at', 'user_id']} | {'assignee': None, 'notice': '回复未建立抽查及当时负责人快照'} for m in unreviewed], 'legacy_quality': [v for v in legacy if not v.get('review_id')]}
        totals = {'unprocessed': {'value': len(open_rows), 'sample_count': len(open_rows), 'unknown_count': sum(not r['assignee'] for r in open_rows), 'stages': stages}, 'overdue': {'value': len(overdue), 'sample_count': len(open_rows), 'unknown_count': sum(not r['staff_due_at'] and not r['platform_deadline'] for r in open_rows), 'staff': _ratio(staff_late, staff_set) | {'unset': len(open_rows) - staff_set}, 'platform': _ratio(platform_late, platform_set) | {'unset': len(open_rows) - platform_set}}, 'repeat_contacts': {'value': len(repeats), 'message_count': len({v['message_id'] for v in repeats}), 'target_count': len({(v['target']['type'], v['target']['id']) for v in repeats}), 'sample_count': len(repeats), 'unknown_count': 0, 'rate': None, 'notice': '员工明确关联条数；首版不推定自动重复咨询率'}, 'quality_reviews': _ratio(errors, judgeable) | {'value': errors, 'sample_count': len(reviews), 'unknown_count': sum(v['outcome'] == 'unknown' for v in reviews), 'no_error': sum(v['outcome'] == 'no_error' for v in reviews), 'with_error': sum(v['outcome'] == 'error' for v in reviews), 'unreviewed': len(unreviewed), 'tag_counts': tag_counts, 'legacy_error_records': legacy_count, 'unknown_assignee_replies': sum(m['id'] not in reviewed for m in replies), 'notice': '事实或政策错答样本 / 可判断抽查样本；不是全部回复准确率'}, 'work_time': {'value': minutes if times else None, 'total_minutes': minutes if times else None, 'entry_count': len(times), 'target_count': len(timed_targets), 'sample_count': len(times), 'unknown_count': len(unknown_time), 'mean_minutes_per_recorded_target': minutes / len(timed_targets) if timed_targets else None}}
        token = db.uid('metrics_snapshot'); expires = (_dt(as_of) + timedelta(minutes=15)).isoformat()
        result = {'as_of': as_of, 'timezone': 'Asia/Shanghai', 'period': {'start': start, 'end': end}, 'filters': {'assignee': assignee, 'include_demo': include_demo}, 'scope_version': VERSION, 'snapshot_token': token, 'snapshot_expires_at': expires, 'metrics': totals, 'detail_counts': {k: len(v) for k, v in details.items()}, 'notice': '本地记录统计；当前未办结与超时包含旧积压，不按日期截断。再次咨询按顾客消息时间、抽查按完成时间、人工耗时按工作日期。历史负责人无快照时显示尚未分配。'}
        c.execute('INSERT INTO operations_metric_snapshots VALUES (?,?,?,?)', (token, user['id'], expires, _json({'summary': result, 'details': details})))
        c.execute('DELETE FROM operations_metric_snapshots WHERE expires_at<?', (as_of,))
        return result


def metric_details(user, token, metric, page=1, page_size=25):
    service.require_staff(user)
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100: _fail(422, '请选择有效分页，每页最多 100 条')
    with work._read() as c:
        row = c.execute('SELECT * FROM operations_metric_snapshots WHERE id=? AND actor_id=?', (token, user['id'])).fetchone()
        if not row or _dt(row['expires_at']) <= _dt(db.now()): _fail(409, '统计已更新，请刷新看板后打开明细')
        v = json.loads(row['data'])
        if metric not in v['details']: _fail(422, '请选择有效指标明细')
        items = v['details'][metric]; return {'items': items[(page - 1) * page_size:page * page_size], 'total': len(items), 'page': page, 'page_size': page_size, 'as_of': v['summary']['as_of'], 'timezone': 'Asia/Shanghai', 'period': v['summary']['period'], 'filters': v['summary']['filters'], 'scope_version': VERSION, 'snapshot_token': token, 'metric': metric}


def original_record(user, target_type, identity):
    """A separate staff-only read for tracing the live origin of snapshot rows."""
    service.require_staff(user)
    with work._read() as c:
        if target_type == 'message':
            message, conv = _message(c, identity, ['user', 'assistant', 'staff'])
            item = {'title': '顾客原话' if message['role'] == 'user' else '真实客服回复', 'user_id': message['user_id'], 'conversation_id': message['conversation_id'], 'order_id': conv.get('selected_order_id')}
            record = message; origin = {'source_messages': [message], 'record_reason': '', 'created_at': message['created_at']}
        else:
            item = work._resolve(c, user, {'type': target_type, 'id': identity}); origin = _origin(c, item)
            if target_type in ['case', 'service_request']:
                record = _load(c, 'cases' if target_type == 'case' else 'service_requests', identity, '原业务事项')
            elif target_type == 'logistics_exception':
                from . import logistics_operations
                value = _load(c, 'logistics_exceptions', identity, '物流异常')
                shipment = _load(c, 'logistics_shipments', value['shipment_id'], '运单')
                record = value | {'shipment': logistics_operations._shipment_view(c, shipment), 'events': logistics_operations._events(c, shipment['id'])}
            else: record = item
        person = _load(c, 'users', item['user_id'], '顾客')
        messages = [service._message(r) for r in c.execute("SELECT * FROM messages WHERE conversation_id=? AND status='completed' ORDER BY rowid DESC LIMIT 200", (item.get('conversation_id'),))] if item.get('conversation_id') else []
        return {'item': {'title': item['title'], 'user_name': person['name'], 'order_id': item.get('order_id'), 'conversation_id': item.get('conversation_id')}, 'record': record, 'origin': origin, 'messages': list(reversed(messages)), 'as_of': db.now(), 'notice': '当前原始记录只供追溯核对；它不会替换看板已保存的汇总或明细快照。'}
