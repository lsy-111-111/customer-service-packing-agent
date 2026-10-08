"""Small, owner-scoped human service records. No payment or shipping execution."""
import hashlib
import json
from fastapi import HTTPException
from . import db

SCHEMA = '''
CREATE TABLE IF NOT EXISTS service_requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,conversation_id TEXT NOT NULL,order_id TEXT,kind TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS service_requests_owner ON service_requests(user_id,status);
CREATE INDEX IF NOT EXISTS service_requests_conversation ON service_requests(conversation_id,status);
CREATE TABLE IF NOT EXISTS service_feedback(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,cycle TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(user_id,target_type,target_id,cycle));
CREATE TABLE IF NOT EXISTS support_operations(user_id TEXT NOT NULL,operation TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(user_id,operation,request_id));
CREATE TABLE IF NOT EXISTS service_quality(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
'''
KINDS = {'repair': '维修受理', 'logistics': '物流核实', 'refund_only': '仅退款受理', 'followup': '未解决跟进'}
TERMINAL = {'completed', 'rejected'}
STAGES = {'submitted': 'pending', 'processing': 'processing', 'needs_info': 'needs_info', 'awaiting_confirmation': 'awaiting_confirmation', 'approved': 'approved'}
QUALITY_TAGS = {'factual_error', 'policy_error', 'missed_intent', 'repeated_question', 'poor_empathy', 'unclear_next_step', 'wrong_promise'}


def init_support():
    with db.connect() as c:
        c.executescript(SCHEMA)


def _fail(code, message):
    raise HTTPException(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _text(value, label, maximum=2000, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        _fail(422, f'请填写有效的{label}（最多 {maximum} 字）')
    return value.strip()


def _role(user, role):
    if user.get('role') != role:
        _fail(403, '此操作需要人工客服身份' if role == 'staff' else '请使用顾客身份操作')


def _owner(user, uid):
    if user.get('role') != 'staff' and user.get('id') != uid:
        _fail(404, '记录不存在或无权访问')


def _row(c, table, identity):
    if not isinstance(identity, str):
        _fail(422, '记录标识格式不正确')
    # Table names are private constants, never user input.
    row = c.execute(f'SELECT * FROM {table} WHERE id=?', (identity,)).fetchone()
    if not row:
        _fail(404, '记录不存在或无权访问')
    return dict(row)


def _request(c, user, identity):
    row = _row(c, 'service_requests', identity)
    _owner(user, row['user_id'])
    return json.loads(row['data'])


def _memo(c, user, operation, payload):
    rid = _text(payload.get('request_id'), '请求标识', 200)
    digest = hashlib.sha256(_json(payload).encode()).hexdigest()
    row = c.execute('SELECT * FROM support_operations WHERE user_id=? AND operation=? AND request_id=?', (user['id'], operation, rid)).fetchone()
    if row:
        if row['fingerprint'] != digest:
            _fail(409, '同一请求标识不能用于不同内容，请刷新后重试')
        return rid, digest, json.loads(row['data'])
    return rid, digest, None


def _remember(c, user, operation, rid, digest, result):
    c.execute('INSERT INTO support_operations VALUES (?,?,?,?,?)', (user['id'], operation, rid, digest, _json(result)))
    return result


def _save(c, item):
    c.execute('INSERT INTO service_requests VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,version=excluded.version,data=excluded.data',
              (item['id'], item['user_id'], item['conversation_id'], item.get('order_id'), item['kind'], item['status'], item['version'], _json(item)))


def _history(item, user, action, note):
    item['updated_at'] = db.now()
    item['history'].append({'action': action, 'note': note, 'actor_id': user['id'], 'actor_role': user['role'], 'at': item['updated_at']})


def _guard(c, user, cid, turn_id):
    if not turn_id:
        return
    turn = _row(c, 'turns', turn_id)
    conversation = _row(c, 'conversations', cid)
    if (turn['user_id'] != user['id'] or turn['conversation_id'] != cid or turn['status'] != 'pending'
            or conversation['mode'] != 'ai' or turn['generation'] != conversation['generation']):
        _fail(409, '对话已切换或本轮已结束，未创建新的受理记录')


def _new(user, cid, kind, reason, order_id=None, **extra):
    stamp = db.now()
    return {'id': db.uid('service'), 'user_id': user['id'], 'conversation_id': cid, 'order_id': order_id,
            'kind': kind, 'status': 'submitted', 'version': 1, 'reason': reason,
            'assigned_staff_id': None, 'proposal': None, 'customer_confirmed_version': None,
            'reviewed_version': None, 'completion_note': '', 'review_note': '', 'history': [],
            'created_at': stamp, 'updated_at': stamp, 'execution_scope': 'manual_record_only', **extra}


def create_request(user, data, guard_turn=None):
    _role(user, 'customer')
    if data.get('explicit_request') is not True:
        _fail(422, '仅在顾客明确要求办理时登记；一般咨询不会创建受理记录')
    kind = data.get('kind')
    if kind not in KINDS or kind == 'followup':
        _fail(422, '请选择维修、物流核实或仅退款受理')
    reason = _text(data.get('reason'), '办理诉求')
    with db.transaction() as c:
        conv = _row(c, 'conversations', data.get('conversation_id'))
        _owner(user, conv['user_id'])
        rid, digest, previous = _memo(c, user, 'create', data)
        if previous:
            return previous
        _guard(c, user, conv['id'], guard_turn)
        order_id = data.get('order_id') or None
        if order_id:
            _owner(user, _row(c, 'orders', order_id)['user_id'])
        source = data.get('source_message_id') or None
        if source:
            msg = _row(c, 'messages', source)
            if msg['conversation_id'] != conv['id'] or msg['role'] != 'user':
                _fail(422, '诉求来源须为当前会话的顾客消息')
        if guard_turn and not source:
            _fail(422, '智能客服登记须保留顾客原始消息来源')
        intent_id = data.get('intent_id') or None
        if intent_id is not None:
            _text(intent_id, '诉求标识', 200)
            intent = _row(c, 'service_intents', intent_id)
            intent_data = json.loads(intent['data'])
            if intent['conversation_id'] != conv['id'] or not source or source not in (intent_data.get('source_message_ids') or [intent['source_message_id']]):
                _fail(422, '诉求必须属于当前会话，并对应提供的顾客原始消息')
        # The same explicit message/intent cannot create parallel copies via new request IDs.
        for row in c.execute("SELECT data FROM service_requests WHERE user_id=? AND conversation_id=? AND kind=? AND status NOT IN ('completed','rejected')", (user['id'], conv['id'], kind)):
            item = json.loads(row[0])
            if item.get('order_id') == order_id and ((intent_id and item.get('intent_id') == intent_id) or (source and item.get('source_message_id') == source) or item['reason'] == reason):
                return _remember(c, user, 'create', rid, digest, item)
        item = _new(user, conv['id'], kind, reason, order_id, source_message_id=source, intent_id=intent_id)
        _history(item, user, 'created', '已登记明确办理诉求，等待人工核实；未执行退款、维修或物流操作。')
        _save(c, item)
        return _remember(c, user, 'create', rid, digest, item)


def get_request(user, identity):
    with db.connect() as c:
        return _request(c, user, identity)


def list_requests(user, conversation_id=None):
    with db.connect() as c:
        if conversation_id:
            _owner(user, _row(c, 'conversations', conversation_id)['user_id'])
        return [json.loads(r['data']) for r in c.execute('SELECT * FROM service_requests ORDER BY rowid DESC')
                if (user['role'] == 'staff' or r['user_id'] == user['id']) and (not conversation_id or r['conversation_id'] == conversation_id)]


def request_action(user, identity, data):
    action = data.get('action')
    _role(user, 'customer' if action in ('confirm', 'supplement') else 'staff')
    with db.transaction() as c:
        item = _request(c, user, identity)
        rid, digest, previous = _memo(c, user, identity, data)
        if previous:
            return previous
        version = data.get('expected_version')
        if isinstance(version, bool) or not isinstance(version, int) or version != item['version']:
            _fail(409, '受理记录已更新，请刷新后核对')
        if item['status'] in TERMINAL:
            _fail(409, '该记录已结束，请通过未解决反馈建立关联跟进')
        note = _text(data.get('note', ''), '处理说明', required=action not in ('claim', 'start', 'confirm'))
        old_confirmed = item.get('customer_confirmed_version') == version
        old_reviewed = item.get('reviewed_version') == version
        next_version = version + 1
        if action in ('claim', 'start'):
            if item['status'] not in ('submitted', 'processing'):
                _fail(409, '当前阶段不能接单，请先完成所需确认或补充材料')
            if item.get('assigned_staff_id') not in (None, user['id']):
                _fail(409, '此受理记录已有负责人')
            item.update(assigned_staff_id=user['id'], status='processing')
            note = note or '人工客服已接单，正在核实。'
        elif action == 'needs_info':
            item.update(status='needs_info', review_note=note)
            old_confirmed = old_reviewed = False
        elif action == 'supplement':
            if item['status'] != 'needs_info':
                _fail(409, '当前记录不在待补充材料阶段')
            item['status'] = 'awaiting_confirmation' if item.get('proposal') else 'submitted'
            old_confirmed = old_reviewed = False
        elif action == 'propose':
            proposal = data.get('proposal')
            if not isinstance(proposal, dict):
                _fail(422, '请填写需要顾客确认的实质方案')
            description = _text(proposal.get('description'), '方案说明')
            amount = proposal.get('amount_cents', 0)
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 or amount > 100_000_000:
                _fail(422, '方案金额应为非负整数分')
            payer = proposal.get('shipping_payer', 'not_applicable')
            if payer not in ('store', 'customer', 'not_applicable'):
                _fail(422, '请明确费用承担方式')
            item.update(status='awaiting_confirmation', proposal={'description': description, 'amount_cents': amount, 'shipping_payer': payer,
                        'customer_action': _text(proposal.get('customer_action', ''), '顾客配合事项', required=False)}, review_note=note)
            old_confirmed = old_reviewed = False
        elif action == 'confirm':
            if item['status'] != 'awaiting_confirmation' or not item.get('proposal'):
                _fail(409, '没有待确认的方案')
            item['status'] = 'submitted'
            old_confirmed, old_reviewed = True, False
            note = '顾客已确认当前展示的方案，等待人工终审。'
        elif action == 'approve':
            if item['status'] not in ('submitted', 'processing') or not item.get('proposal') or not old_confirmed:
                _fail(409, '实质方案须先由顾客确认当前版本，再由人工审核')
            item.update(status='approved', assigned_staff_id=user['id'], review_note=note)
            old_reviewed = True
        elif action == 'reject':
            item.update(status='rejected', assigned_staff_id=user['id'], review_note=note)
        elif action == 'complete':
            material = bool(item.get('proposal')) or item['kind'] == 'refund_only'
            if material and (item['status'] != 'approved' or not old_confirmed or not old_reviewed):
                _fail(409, '涉及退款或实质方案，须完成顾客确认和人工审核后才能记录完成')
            if not material and item['status'] not in ('submitted', 'processing', 'needs_info'):
                _fail(409, '请先完成当前受理步骤')
            item.update(status='completed', assigned_staff_id=user['id'], completion_note=note)
        else:
            _fail(422, '不支持的受理操作')
        item['version'] = next_version
        item['customer_confirmed_version'] = next_version if old_confirmed else None
        item['reviewed_version'] = next_version if old_reviewed else None
        _history(item, user, action, note)
        _save(c, item)
        return _remember(c, user, identity, rid, digest, item)


def _feedback_target(c, user, target_type, identity):
    if target_type == 'service_request':
        target = _request(c, user, identity)
    elif target_type == 'case':
        row = _row(c, 'cases', identity)
        _owner(user, row['user_id'])
        target = json.loads(row['data'])
    else:
        _fail(422, '仅支持对已完成的售后或人工受理记录评价')
    if target['status'] != 'completed':
        _fail(409, '处理完成后才可评价；完成状态本身不代表顾客满意')
    root = f'{target_type}:{identity}'
    linked = [json.loads(r[0]) for r in c.execute("SELECT data FROM service_requests WHERE user_id=? AND kind='followup' ORDER BY rowid DESC", (target['user_id'],))]
    latest = next((i for i in linked if i.get('followup_root') == root), None)
    # Open followups share a feedback cycle; a completed followup permits a fresh round.
    cycle = str(target['version'])
    if latest:
        cycle = latest.get('feedback_cycle', cycle) if latest['status'] not in TERMINAL else f"{target['version']}:{latest['id']}:{latest['version']}"
    return target, root, cycle, latest


def _linked_intent_ids(target):
    """Only persisted server bindings may carry resolution across a workflow."""
    ids = target.get('intent_ids', [])
    ids = ids if isinstance(ids, list) else []
    return list(dict.fromkeys(i for i in [target.get('intent_id'), *ids] if isinstance(i, str) and i))


def get_feedback(user, target_type, identity):
    with db.connect() as c:
        target, _, cycle, followup = _feedback_target(c, user, target_type, identity)
        row = c.execute('SELECT data FROM service_feedback WHERE user_id=? AND target_type=? AND target_id=? AND cycle=?', (target['user_id'], target_type, identity, cycle)).fetchone()
        feedback = json.loads(row[0]) if row else None
        return {'resolution': feedback['resolution'] if feedback else 'unknown', 'score': feedback.get('score') if feedback else None, 'feedback': feedback, 'followup': followup, 'cycle': cycle}


def submit_feedback(user, data):
    _role(user, 'customer')
    resolution = data.get('resolution')
    if resolution not in ('solved', 'unresolved'):
        _fail(422, '请选择已解决或未解决')
    score = data.get('score')
    if score is not None and (isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5):
        _fail(422, '评分应为 1–5，未评分可留空')
    note = _text(data.get('note', ''), '反馈说明', required=False)
    with db.transaction() as c:
        rid, digest, previous = _memo(c, user, 'feedback', data)
        if previous:
            return previous
        typ, identity = data.get('target_type'), data.get('target_id')
        target, root, cycle, latest = _feedback_target(c, user, typ, identity)
        row = c.execute('SELECT data FROM service_feedback WHERE user_id=? AND target_type=? AND target_id=? AND cycle=?', (user['id'], typ, identity, cycle)).fetchone()
        if row:
            result = json.loads(row[0])
            return _remember(c, user, 'feedback', rid, digest, result)
        followup = None
        if resolution == 'unresolved':
            if latest and latest['status'] not in TERMINAL:
                followup = latest
            else:
                followup = _new(user, target['conversation_id'], 'followup', note or '顾客反馈原处理仍未解决，请核对未解决原因并继续接续。', target.get('order_id'),
                                followup_root=root, feedback_cycle=cycle, parent_type=typ, parent_id=identity,
                                intent_id=target.get('intent_id'), intent_ids=_linked_intent_ids(target))
                _history(followup, user, 'feedback_followup', '顾客反馈未解决，已建立关联跟进；原处理结果保留。')
                _save(c, followup)
        result = {'id': db.uid('feedback'), 'user_id': user['id'], 'target_type': typ, 'target_id': identity, 'cycle': cycle,
                  'resolution': resolution, 'score': score, 'note': note, 'followup_request_id': followup['id'] if followup else None, 'created_at': db.now(), 'version': 1}
        c.execute('INSERT INTO service_feedback VALUES (?,?,?,?,?,?)', (result['id'], user['id'], typ, identity, cycle, _json(result)))
        intent_ids = _linked_intent_ids(target)
        if intent_ids and c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_intents'").fetchone():
            c.executemany('UPDATE service_intents SET resolution=?,updated_at=? WHERE id=? AND conversation_id=?',
                          [(resolution, db.now(), identity, target['conversation_id']) for identity in intent_ids])
        return _remember(c, user, 'feedback', rid, digest, result)


def _intents(c, cid):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_intents'").fetchone():
        return []
    cols = {r[1] for r in c.execute('PRAGMA table_info(service_intents)')}
    if not {'conversation_id', 'data'}.issubset(cols):
        return []
    result = []
    for row in c.execute('SELECT * FROM service_intents WHERE conversation_id=?', (cid,)):
        try:
            value = json.loads(row['data'])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            value = {**value, **{key: row[key] for key in ('id', 'status', 'resolution', 'source_message_id') if key in cols}}
        result.extend(value if isinstance(value, list) else value.get('items', [value]) if isinstance(value, dict) else [])
    return [i for i in result if isinstance(i, dict)]


def _same_remedy(intent, item):
    """An order/channel match cannot collapse independent after-sale remedies."""
    from .conversation_state import request_types
    types = [intent['request_type']] if intent.get('request_type') else request_types(intent.get('text', ''))
    return types == [item.get('request_type')]


def _work_items(c, user):
    items = []
    for table, typ in [('cases', 'case'), ('service_requests', 'service_request')]:
        for row in c.execute(f'SELECT * FROM {table} ORDER BY rowid DESC'):
            if user['role'] != 'staff' and row['user_id'] != user['id']:
                continue
            item = json.loads(row['data'])
            if item['status'] not in STAGES:
                continue
            conv = _row(c, 'conversations', item['conversation_id'])
            person = json.loads(_row(c, 'users', item['user_id'])['data'])
            items.append({'id': item['id'], 'type': typ, 'stage': STAGES[item['status']], 'status': item['status'], 'conversation_id': item['conversation_id'],
                          'user_id': item['user_id'], 'user_name': person['name'], 'order_id': item.get('order_id'), 'intent_id': item.get('intent_id'), 'intent_ids': _linked_intent_ids(item),
                          'source_message_id': item.get('source_message_id'),
                          'request_type': item.get('type') if typ == 'case' else item.get('kind'),
                          'title': KINDS.get(item.get('kind'), {'return_refund': '退货退款', 'exchange': '换货', 'reship': '补发'}.get(item.get('type'), '售后申请')),
                          'reason': item.get('reason', ''), 'updated_at': item.get('updated_at', conv['updated_at']), 'assigned_staff_id': item.get('assigned_staff_id')})
    for conv in c.execute("SELECT * FROM conversations WHERE mode='waiting_human' ORDER BY updated_at DESC"):
        if user['role'] != 'staff' and conv['user_id'] != user['id']:
            continue
        related = [i for i in items if i['conversation_id'] == conv['id']]
        intents = _intents(c, conv['id'])
        human = [i for i in intents if i.get('status') in ('handoff', 'handed_off', 'human', 'needs_human') or i.get('disposition') in ('human', 'handoff')]
        obligations = []
        for intent in human:
            order_ids = list(dict.fromkeys(intent.get('order_ids', []))) if intent.get('kind') == 'after_sale' else []
            if len(order_ids) > 1:
                # One sentence can bind several orders. Queue obligations are
                # per order without changing the persistent conversation intent.
                obligations.extend({**intent, 'order_ids': [oid], '_queue_order_id': oid} for oid in order_ids)
            else:
                obligations.append(intent)
        pending = []
        for intent in obligations:
            if intent.get('kind') == 'manual':
                continue  # Channel request, not an additional business obligation.
            order_ids = intent.get('order_ids', []) if intent.get('kind') == 'after_sale' else []
            candidates = [r for r in related if not order_ids or r.get('order_id') in order_ids]
            matches = [r for r in candidates if intent.get('id') in r.get('intent_ids', [])
                       and (intent.get('kind') != 'after_sale' or r.get('request_type') == 'followup' or _same_remedy(intent, r))]
            if not matches and intent.get('kind') == 'after_sale':
                matches = [r for r in candidates if _same_remedy(intent, r) and
                           ((r.get('source_message_id') and r['source_message_id'] in intent.get('source_message_ids', []))
                            or (r.get('order_id') and r['order_id'] in intent.get('order_ids', [])))]
            if matches:
                matches[0]['needs_handoff'] = True
            else:
                pending.append(intent)
        if related and not pending:
            if not any(i.get('needs_handoff') for i in related):
                related[0]['needs_handoff'] = True
        else:
            person = json.loads(_row(c, 'users', conv['user_id'])['data'])
            # A chat is a channel; independent unbound obligations remain
            # separate queue items even when they open the same conversation.
            for intent in pending or [None]:
                identity = (intent.get('id') or hashlib.sha256(_json(intent).encode()).hexdigest()[:16]) if intent else None
                if intent and intent.get('_queue_order_id'):
                    identity = f"{identity}:{intent['_queue_order_id']}"
                order_ids = intent.get('order_ids', []) if intent else []
                items.append({'id': f"human:{conv['id']}:{identity}" if identity else f"human:{conv['id']}",
                              'type': 'conversation', 'stage': 'waiting_human', 'status': 'waiting_human',
                              'conversation_id': conv['id'], 'user_id': conv['user_id'], 'user_name': person['name'], 'title': '等待人工接入',
                              'reason': (intent.get('text') or intent.get('summary') or '待接续诉求') if intent else '顾客已请求人工服务',
                              'order_id': order_ids[0] if len(order_ids) == 1 else None,
                              'request_type': intent.get('request_type') if intent else None,
                              'updated_at': conv['updated_at'], 'intent_id': intent.get('id') if intent else None,
                              'intent_ids': [intent['id']] if intent and intent.get('id') else [], 'needs_handoff': True})
    return sorted(items, key=lambda i: i['updated_at'], reverse=True)


def work_items(user, stage='all', conversation_id=None):
    _role(user, 'staff')
    if stage not in ('all', 'pending', 'processing', 'needs_info', 'awaiting_confirmation', 'approved', 'waiting_human'):
        _fail(422, '不支持的待办阶段')
    with db.connect() as c:
        items = _work_items(c, user)
        counts = {key: sum(i['stage'] == key for i in items) for key in (*STAGES.values(), 'waiting_human')}
        counts['waiting_human'] = sum(bool(i.get('needs_handoff')) for i in items)
        selected = [i for i in items if (stage == 'all' or (i.get('needs_handoff') if stage == 'waiting_human' else i['stage'] == stage)) and (not conversation_id or i['conversation_id'] == conversation_id)]
        return {'items': selected, 'total': len(items), 'stage_counts': counts, 'filtered_total': len(selected)}


def service_metrics(user):
    """All-time local business counts, feedback-target counts and feedback cycles.

    No synthetic satisfaction: absent feedback stays in the eligible target
    denominator, while an absent score is excluded from the rating average.
    """
    _role(user, 'staff')
    with db.connect() as c:
        c.execute('BEGIN')
        records = {(typ, row['id']): json.loads(row['data'])
                   for table, typ in (('cases', 'case'), ('service_requests', 'service_request'))
                   for row in c.execute(f'SELECT id,data FROM {table}')}
        cycles = [json.loads(row[0]) for row in c.execute('SELECT data FROM service_feedback ORDER BY rowid')]
    business = {key: item for key, item in records.items() if item['status'] != 'draft'}
    eligible = {key for key, item in business.items() if item['status'] == 'completed'}
    latest = {}
    for feedback in cycles:
        latest[(feedback['target_type'], feedback['target_id'])] = feedback
    resolved = sum(latest.get(key, {}).get('resolution') == 'solved' for key in eligible)
    unresolved_targets = sum(latest.get(key, {}).get('resolution') == 'unresolved' for key in eligible)
    unresolved = [f for f in cycles if f['resolution'] == 'unresolved']
    linked = 0
    for feedback in unresolved:
        followup = records.get(('service_request', feedback.get('followup_request_id')), {})
        if (followup.get('kind') == 'followup' and followup.get('parent_type') == feedback['target_type']
                and followup.get('parent_id') == feedback['target_id']):
            linked += 1
    scores = [f['score'] for f in cycles if type(f.get('score')) is int and 1 <= f['score'] <= 5]

    def ratio(numerator, denominator):
        return {'numerator': numerator, 'denominator': denominator, 'rate': numerator / denominator if denominator else None}

    return {'as_of': db.now(), 'period': 'all_time',
            'business_completion': {**ratio(len(eligible), len(business)),
                                    'rejected': sum(item['status'] == 'rejected' for item in business.values()),
                                    'followup_records': sum(item.get('kind') == 'followup' for item in business.values())},
            'customer_resolution': {**ratio(resolved, len(eligible)), 'unresolved_targets': unresolved_targets,
                                    'unknown_targets': len(eligible) - resolved - unresolved_targets},
            'unresolved_followup': ratio(linked, len(unresolved)),
            'rating': {'score_sum': sum(scores), 'scored_cycles': len(scores),
                       'mean': sum(scores) / len(scores) if scores else None,
                       'unscored_cycles': len(cycles) - len(scores), 'feedback_cycles': len(cycles)}}


def _conversation_support(user, cid):
    with db.connect() as c:
        conv = _row(c, 'conversations', cid)
        _owner(user, conv['user_id'])
        requests = [json.loads(r[0]) for r in c.execute('SELECT data FROM service_requests WHERE conversation_id=? ORDER BY rowid DESC', (cid,))]
        messages = [dict(r) for r in c.execute("SELECT id,role,content,created_at FROM messages WHERE conversation_id=? AND status NOT IN ('failed','cancelled') ORDER BY created_at DESC", (cid,))]
        cases = []
        for row in c.execute('SELECT data FROM cases WHERE conversation_id=? AND user_id=? ORDER BY rowid DESC', (cid, conv['user_id'])):
            try:
                cases.append(json.loads(row[0]))
            except (ValueError, TypeError):
                continue
        intents = _intents(c, cid)
        source_ids = {source for intent in intents for source in intent.get('source_message_ids', []) if isinstance(source, str)}
        source_ids.update(r['source_message_id'] for r in requests if r.get('source_message_id'))
        source_ids.update(m['id'] for m in messages if m['role'] == 'user')
        claims = [{'text': m['content'], 'source_message_id': m['id'], 'source': 'customer_statement', 'updated_at': m['created_at']} for m in messages if m['role'] == 'user' and m['id'] in source_ids]
        for request in requests:
            if not request.get('source_message_id') and request.get('reason'):
                claims.append({'text': request['reason'], 'source_message_id': request['id'], 'source': 'customer_intake', 'updated_at': request.get('created_at')})
        order_ids = list(dict.fromkeys([r['order_id'] for r in [*requests, *cases] if r.get('order_id')] + ([conv['selected_order_id']] if conv.get('selected_order_id') else [])))
        verified = []
        for oid in order_ids:
            row = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            if not row:
                continue
            if row['user_id'] != conv['user_id']:
                continue
            order = json.loads(row['data'])
            verified.append({'text': f"订单 {oid} · {order.get('product_name', '')} · {order.get('status', '')}", 'source': 'order_record', 'source_id': oid, 'updated_at': order.get('updated_at', order.get('created_at'))})
        case_records = []
        for case in cases:
            for field, fact in case.get('facts', {}).items():
                if isinstance(fact, dict) and fact.get('source') in ('system', 'system_record', 'staff', 'staff_verified'):
                    verified.append({'text': f"{field}：{fact.get('value')}（{fact.get('source')}）", 'source': 'case_fact', 'source_id': case['id'], 'updated_at': case.get('updated_at')})
            case_records.append({'source_id': case['id'], 'type': case.get('type'), 'status': case.get('status'), 'reason': case.get('reason', ''),
                                 'condition_note': case.get('condition_note', ''), 'review_note': case.get('review_note', ''), 'completion_note': case.get('completion_note', ''),
                                 'history': case.get('history', []), 'updated_at': case.get('updated_at')})
        unanswered = [i for i in intents if i.get('status') in ('pending', 'needs_info', 'handed_off')]
        # Goals are projections of persisted active obligations, not another
        # interpretation of the raw conversation. Keep form-only records useful
        # even when they have no chat message, and retain exact source links.
        labels = {**KINDS, 'return_refund': '退货退款', 'exchange': '换货', 'reship': '补发',
                  'price': '查询价格', 'stock': '查询库存', 'compare': '比较商品',
                  'recommend': '选购建议', 'question': '咨询问题', 'status': '查询处理进度',
                  'manual': '人工接续', 'after_sale': '售后咨询'}
        business = [('case', item) for item in cases] + [('service_request', item) for item in requests]
        related_records = [{'id': item['id'], 'type': typ, 'label': labels.get(item.get('type') if typ == 'case' else item.get('kind'), '服务记录'),
                            'status': item['status']} for typ, item in business]
        open_business = [(typ, item) for typ, item in business if item.get('status') in {'draft', *STAGES}]
        user_messages = {m['id']: m for m in messages if m['role'] == 'user'}
        owned_orders = {row[0] for row in c.execute('SELECT id FROM orders WHERE user_id=?', (conv['user_id'],))}
        intent_by_id = {item['id']: item for item in intents if item.get('id')}

        def source_messages(item, linked=()):
            candidates = [item.get('latest_message_id'), item.get('source_message_id'), *item.get('source_message_ids', [])]
            for identity in linked:
                prior = intent_by_id.get(identity, {})
                candidates.extend([prior.get('latest_message_id'), prior.get('source_message_id'), *prior.get('source_message_ids', [])])
            return list(dict.fromkeys(identity for identity in candidates if isinstance(identity, str) and identity in user_messages))

        goals, represented = [], set()
        for intent in unanswered:
            if not intent.get('id') or intent.get('resolution') == 'solved':
                continue
            linked = []
            for typ, item in open_business:
                if intent['id'] not in _linked_intent_ids(item):
                    continue
                request_type = item.get('type') if typ == 'case' else item.get('kind')
                if intent.get('kind') == 'after_sale' and request_type != 'followup' and not _same_remedy(intent, {'request_type': request_type}):
                    continue
                if intent.get('order_ids') and item.get('order_id') not in intent['order_ids']:
                    continue
                linked.append((typ, item))
                represented.add((typ, item['id']))
            sources = source_messages(intent)
            kind = intent.get('request_type') or intent.get('kind')
            label = labels.get(kind, '待接续诉求')
            text = intent.get('text') or intent.get('summary') or label
            goals.append({'id': 'intent:' + intent['id'], 'source_type': 'intent', 'text': f'{label}：{text}',
                          'status': intent['status'], 'source_message_id': sources[0] if sources else None,
                          'source_message_ids': sources,
                          'order_ids': [oid for oid in intent.get('order_ids', []) if oid in owned_orders],
                          'intent_ids': [intent['id']], 'request_ids': [item['id'] for typ, item in linked if typ == 'service_request'],
                          'case_ids': [item['id'] for typ, item in linked if typ == 'case'],
                          'updated_at': intent.get('updated_at') or (user_messages[sources[0]]['created_at'] if sources else None)})
        for typ, item in open_business:
            if (typ, item['id']) in represented:
                continue
            linked = [identity for identity in _linked_intent_ids(item) if identity in intent_by_id]
            sources = source_messages(item, linked)
            kind = item.get('type') if typ == 'case' else item.get('kind')
            label = labels.get(kind, '服务请求')
            goals.append({'id': typ + ':' + item['id'], 'source_type': typ,
                          'text': f"{label}：{item.get('reason') or label}", 'status': item['status'],
                          'source_message_id': sources[0] if sources else None, 'source_message_ids': sources,
                          'order_ids': [item['order_id']] if item.get('order_id') in owned_orders else [],
                          'intent_ids': linked, 'request_ids': [item['id']] if typ == 'service_request' else [],
                          'case_ids': [item['id']] if typ == 'case' else [], 'updated_at': item.get('updated_at')})
        actions = [{**h, 'source_id': r['id'], 'at': h.get('at') or h.get('created_at') or r.get('updated_at', '')} for r in [*requests, *cases] for h in r.get('history', []) if isinstance(h, dict)]
        last_action = max(actions, key=lambda h: h['at']) if actions else None
        summary = {'goals': goals, 'related_records': related_records, 'claims': claims, 'verified': verified, 'unanswered': unanswered, 'last_action': last_action,
                   'next_steps': [{'request_id': r['id'], 'status': r['status'], 'text': {'draft': '请顾客核对申请后提交人工审核', 'submitted': '人工核实并给出处理意见', 'processing': '负责人继续核实', 'needs_info': r.get('review_note') or '请顾客补充材料', 'awaiting_confirmation': '请顾客核对并确认方案', 'approved': '人工记录实际处理结果'}.get(r['status'], '')} for r in [*requests, *cases] if r['status'] not in TERMINAL],
                   'case_records': case_records, 'attempted_actions': sorted(actions, key=lambda h: h['at'], reverse=True), 'updated_at': db.now(),
                   'source_message_ids': [i['source_message_id'] for i in claims]}
        return {'requests': requests, 'summary': summary}


def conversation_support(user, cid):
    # Authorization is outside the optional-summary fallback: errors cannot hide access violations.
    with db.connect() as c:
        _owner(user, _row(c, 'conversations', cid)['user_id'])
    try:
        return _conversation_support(user, cid)
    except (ValueError, TypeError, KeyError, IndexError):
        requests = []
        with db.connect() as c:
            for row in c.execute('SELECT data FROM service_requests WHERE conversation_id=? ORDER BY rowid DESC', (cid,)):
                try:
                    value = json.loads(row[0])
                    if isinstance(value, dict):
                        requests.append(value)
                except (ValueError, TypeError):
                    pass
        return {'requests': requests, 'summary': {'goals': [], 'related_records': [], 'claims': [], 'verified': [], 'unanswered': [], 'last_action': None, 'next_steps': [],
                'source_message_ids': [], 'case_records': [], 'attempted_actions': [], 'updated_at': db.now(),
                'error': '部分历史记录暂时无法整理摘要，请查看原始对话和售后记录继续处理。'}}


def update_feedback(user, identity, data):
    """Optional score/reason never gates or reverses the already-created followup."""
    _role(user, 'customer')
    with db.transaction() as c:
        row = _row(c, 'service_feedback', identity)
        _owner(user, row['user_id'])
        rid, digest, old = _memo(c, user, f'feedback:{identity}', data)
        if old:
            return old
        feedback = json.loads(row['data'])
        if isinstance(data.get('expected_version'), bool) or data.get('expected_version') != feedback.get('version', 1):
            _fail(409, '反馈已更新，请刷新后重试')
        score = data.get('score', feedback.get('score'))
        if score is not None and (isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5):
            _fail(422, '评分应为 1–5，未评分可留空')
        feedback.update(score=score, note=_text(data.get('note', feedback.get('note', '')), '反馈说明', required=False), version=feedback.get('version', 1) + 1)
        c.execute('UPDATE service_feedback SET data=? WHERE id=?', (_json(feedback), identity))
        return _remember(c, user, f'feedback:{identity}', rid, digest, feedback)


def record_quality(user, data):
    _role(user, 'staff')
    tags = data.get('tags')
    if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or t not in QUALITY_TAGS for t in tags):
        _fail(422, '请至少选择一个有效的服务错误标签')
    note = _text(data.get('note', ''), '质检说明', required=False)
    with db.transaction() as c:
        typ = data.get('target_type')
        if typ not in ('case', 'service_request'):
            _fail(422, '不支持的质检对象')
        _row(c, 'cases' if typ == 'case' else 'service_requests', data.get('target_id'))
        rid, digest, old = _memo(c, user, 'quality', data)
        if old:
            return old
        result = {'id': db.uid('quality'), 'actor_id': user['id'], 'target_type': typ, 'target_id': data['target_id'], 'tags': sorted(set(tags)), 'note': note, 'created_at': db.now()}
        c.execute('INSERT INTO service_quality VALUES (?,?,?,?,?,?)', (result['id'], user['id'], typ, data['target_id'], _json(result), result['created_at']))
        return _remember(c, user, 'quality', rid, digest, result)
