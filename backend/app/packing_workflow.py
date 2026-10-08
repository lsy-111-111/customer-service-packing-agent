"""Versioned packing changes, business review and immutable before-images."""
import copy
import json
from fastapi import APIRouter, Body, Depends
from . import db, service, packing

DEFAULT_LAYOUT = {'paper': 'a4', 'kind': 'all', 'width_mm': 100, 'height_mm': 150, 'show_note': True}


def init_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS packing_revisions(task_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(task_id,revision))')
    c.execute('CREATE TABLE IF NOT EXISTS packing_changes(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,data TEXT NOT NULL)')


def layout(data):
    if not isinstance(data, dict):
        service.fail(422, '请核对标签版式')
    result = {**DEFAULT_LAYOUT, **{k: data[k] for k in DEFAULT_LAYOUT if k in data}}
    if result['paper'] not in ('a4', 'label') or result['kind'] not in ('all', 'main', 'side', 'internal'):
        service.fail(422, '请选择支持的纸张和标签内容')
    packing._number(result['width_mm'], '标签宽度', 200, 100)
    packing._number(result['height_mm'], '标签高度', 270, 150)
    if type(result['show_note']) is not bool:
        service.fail(422, '请选择是否打印发货备注')
    return result


def save_revision(c, task, reason):
    snapshot = copy.deepcopy(task)
    snapshot.pop('documents', None)
    c.execute('INSERT OR IGNORE INTO packing_revisions VALUES(?,?,?,?,?)',
              (task['id'], task['revision'], service.js(snapshot), reason, db.now()))


def content_fingerprint(task):
    keys = ('supplier', 'destination', 'ship_date', 'platform_order', 'note', 'lines', 'materials', 'layout', 'revision', 'intake_snapshot')
    value = {key: task.get(key) for key in keys}
    value['boxes'] = [{k: box.get(k) for k in ('id', 'number', 'line_index', 'planned_quantity', 'actual_quantity', 'net_weight', 'gross_weight', 'serial_numbers')}
                      for box in task['boxes']]
    return packing._fingerprint(value)


def approval_preview(c, task, data):
    identity = data.get('preview_document_id')
    if identity:
        rows = c.execute('SELECT data FROM packing_documents WHERE id=? AND task_id=?', (identity, task['id'])).fetchall()
    else:
        rows = c.execute('SELECT data FROM packing_documents WHERE task_id=? ORDER BY rowid DESC', (task['id'],)).fetchall()
    fingerprint = content_fingerprint(task)
    expected_layout = layout(task.get('layout', {}))
    expected_layout['kind'] = 'all'
    for row in rows:
        doc = json.loads(row[0])
        if (doc.get('mode') == 'draft' and doc.get('kind') == 'all' and doc.get('content_fingerprint') == fingerprint
                and doc.get('revision') == task['revision']
                and set(doc.get('box_numbers', [])) == {box['number'] for box in task['boxes']}
                and doc.get('layout') == expected_layout):
            return doc['id']
    service.fail(422, '审核前请生成并核对当前全部箱子的完整草稿PDF；资料或版式修改后须重新预览')


def check_approval(c, task):
    from . import packing_sources
    # Completed shipments retain their original reviewed facts and files.
    # Current SKU controls must gate every unfinished review/release path.
    if task.get('stage') not in ('dispatched', 'archived', 'cancelled'):
        packing.packing_sku.validate_task(c, task)
        packing_sources.validate_for_approval(c, task)
    if task.get('approval') and task['approval'].get('content_fingerprint'):
        if task['approval']['content_fingerprint'] != content_fingerprint(task):
            service.fail(409, '装箱内容与已审核版本不一致，请重新核对并审核')
    pending = c.execute('SELECT data FROM packing_changes WHERE task_id=?', (task['id'],)).fetchall()
    if any(json.loads(row[0]).get('status') == 'pending' for row in pending):
        service.fail(409, '存在待确认的订单变更，请先处理后再打印或出库')


def differences(before, after):
    result = []
    labels = {'supplier': '发货单位', 'destination': '收货仓库／地址', 'ship_date': '计划发货日期', 'platform_order': '客户订单号', 'note': '发货备注',
              'paper': '打印纸张', 'width_mm': '标签宽度（毫米）', 'height_mm': '标签高度（毫米）', 'show_note': '打印发货备注', 'kind': '打印内容'}
    display = {'a4': 'A4纸排版', 'label': '标签纸', 'all': '全部标签', 'main': '正唛', 'side': '侧唛', 'internal': '内部核验标签', True: '是', False: '否'}
    for key, title in labels.items():
        if before.get(key) != after.get(key):
            a, b = before.get(key), after.get(key)
            result.append({'label': title, 'before': display.get(a, a) if isinstance(a, (str, bool)) else a,
                           'after': display.get(b, b) if isinstance(b, (str, bool)) else b})
    line_keys = {'sku_id': '商品编号', 'external_code': '客户商品编码', 'product_name': '商品', 'specification': '规格', 'quantity': '发货数量', 'per_carton': '每箱数量', 'batch': '批次',
                 'produced_at': '生产日期', 'expires_at': '到期日期', 'product_barcode': '商品条码', 'serial_numbers': '序列号',
                 'require_batch': '批次要求', 'require_expiry': '效期要求', 'require_serials': '逐件追溯要求', 'kit_note': '套装说明'}
    for i in range(max(len(before.get('lines', [])), len(after.get('lines', [])))):
        a = before.get('lines', [])[i] if i < len(before.get('lines', [])) else {}
        b = after.get('lines', [])[i] if i < len(after.get('lines', [])) else {}
        for key, title in line_keys.items():
            if a.get(key) != b.get(key):
                result.append({'label': f'第{i+1}行{title}', 'before': a.get(key), 'after': b.get(key)})
    return result


def _requested_after_dispatch(change):
    # The task may have progressed since this request. Its current stage cannot
    # change the meaning of a previously confirmed/withdrawn preview.
    requested_stage = change.get('request_stage')
    if requested_stage:
        return requested_stage in ('dispatched', 'archived')
    # Legacy resolved records can only originate from requires_followup;
    # resolving a pre-dispatch pending request produces withdrawn instead.
    return change.get('status') in ('requires_followup', 'resolved')


def _source_version_caption(change, original):
    version = (change.get('proposed') or {}).get('intake_version')
    if version is None:
        return original  # Do not reconstruct a missing source version.
    if _requested_after_dispatch(change):
        return f'核对第{version}版并登记人工跟进；原审核、标签及出库记录保持不变'
    status = change.get('status')
    if status == 'pending':
        return f'重新核对第{version}版；确认变更后原审核与标签失效'
    if status == 'confirmed':
        return f'已按第{version}版确认变更；确认时原审核与旧标签已失效'
    if status == 'withdrawn':
        return f'曾拟核对第{version}版；本次变更未确认且已撤销'
    if status == 'superseded':
        return f'曾拟核对第{version}版；本次变更未确认，已有其他变更生效'
    return original


def display_change(change):
    """Explain a request without migrating its original data or audit history.

    Projection also happens after idempotency lookup so old cached responses do
    not republish the contradictory post-dispatch caption. Corrections retain
    the original value and an exact path for staff/audit consumers.
    """
    result = copy.deepcopy(change)
    corrections = result.setdefault('display_corrections', [])
    basis = {'request_stage': change.get('request_stage'), 'status': change.get('status'),
             'after_dispatch_request': _requested_after_dispatch(change)}
    def explain(path, original, displayed):
        if original != displayed:
            corrections.append({'path': path, 'original': copy.deepcopy(original),
                                'displayed': copy.deepcopy(displayed), 'basis': basis})
        return displayed
    for index, item in enumerate(result.get('differences', [])):
        if item.get('label') == '原资料核对版本':
            original = item.get('after')
            item['after'] = explain(f'/differences/{index}/after', original, _source_version_caption(change, original))
    impact = result.get('impact', [])
    if _requested_after_dispatch(change):
        impact = ['原出库及归档记录保持不变', '原审核及原标签保持不变；本次申请另行登记人工跟进',
                  '请人工核实拦截、退回或补发；处理结果仅为人工记录；本系统尚未执行物流操作']
    elif change.get('status') == 'confirmed':
        impact = ['确认变更时原审核及全部旧标签已失效', '新版本审核及标签的后续状态请核对对应版本记录']
    elif change.get('status') == 'withdrawn':
        impact = ['本次变更未确认且已撤销', '未因本次申请撤销原审核或标签；请核对其他变更记录']
    elif change.get('status') == 'superseded':
        impact = ['本次变更未确认，已有其他变更生效', '其他变更对审核及标签的影响，请核对其对应记录']
    result['impact'] = explain('/impact', change.get('impact', []), impact)
    if not corrections:
        result.pop('display_corrections', None)
    return result


def list_changes(user, identity):
    packing._staff(user)
    with db.connect() as c:
        packing._get(c, identity)
        return [display_change(json.loads(row[0])) for row in c.execute('SELECT data FROM packing_changes WHERE task_id=? ORDER BY rowid DESC', (identity,))]


def preview_change(user, identity, data):
    packing._staff(user)
    with db.transaction() as c:
        def action():
            task = packing._get(c, identity)
            packing._version(task, data)
            kind = data.get('change_type', 'revise')
            if kind not in ('revise', 'cancel', 'layout'):
                service.fail(422, '请选择修改资料、修改版式或取消订单')
            if task['stage'] == 'cancelled':
                service.fail(409, '这项任务已取消，请新建装箱任务')
            after_dispatch = task['stage'] in ('dispatched', 'archived')
            reason = packing._text(data.get('reason'), '变更原因', 500)
            if not reason:
                service.fail(422, '请填写变更原因并核对客户最新通知')
            proposed = data.get('proposed', {})
            if not isinstance(proposed, dict):
                service.fail(422, '请提供完整变更资料')
            if kind == 'revise':
                normalized = packing._normalize(c, proposed)
                delta = differences(task, normalized)
                previous_source = task.get('intake_snapshot') or {}
                if proposed.get('intake_id') and (proposed.get('intake_id') != previous_source.get('intake_id') or proposed.get('intake_version') != previous_source.get('intake_version')):
                    delta.append({'label': '原资料核对版本', 'before': f'第{previous_source["intake_version"]}版' if previous_source.get('intake_version') else '手工录入',
                                  'after': _source_version_caption({'request_stage': task['stage'], 'status': 'requires_followup' if after_dispatch else 'pending',
                                                                    'proposed': proposed}, '资料版本待核对')})
            elif kind == 'layout':
                normalized = layout(proposed)
                delta = differences(layout(task.get('layout', {})), normalized)
            else:
                normalized = {}
                delta = [{'label': '订单安排', 'before': task['stage_label'], 'after': '申请取消'}]
            if not delta:
                service.fail(422, '本次内容与原记录相同，无需建立变更')
            change = {'id': db.uid('packingchange'), 'task_id': identity, 'task_version': task['version'], 'base_revision': task['revision'],
                      'request_stage': task['stage'],
                      'change_type': kind, 'reason': reason, 'source_note': packing._text(data.get('source_note'), '变更资料出处', 1000),
                      'status': 'requires_followup' if after_dispatch else 'pending', 'version': 1,
                      'created_at': db.now(), 'operator': user['name'], 'differences': delta, 'proposed': proposed,
                      'normalized': normalized, 'impact': ['原出库及归档记录保持不变', '原审核及原标签保持不变；本次申请另行登记人工跟进',
                                                         '请人工核实拦截、退回或补发；处理结果仅为人工记录；本系统尚未执行物流操作'] if after_dispatch else
                      ['确认后原审核与全部旧标签失效', '请撤下已打印的旧标签', '重新核对实装、预览PDF、审核并逐箱扫码'], 'history': []}
            c.execute('INSERT INTO packing_changes VALUES(?,?,?)', (change['id'], identity, service.js(change)))
            service.audit(c, user, 'packing_change_requested', identity, None, {'change_id': change['id'], 'status': change['status']})
            return change
        return display_change(packing._operation(c, user, 'packing_change_preview:' + identity, data, action))


def confirm_change(user, identity, change_id, data):
    packing._staff(user)
    with db.transaction() as c:
        def action():
            task = packing._get(c, identity)
            change = json.loads(service.one(c, 'SELECT data FROM packing_changes WHERE id=? AND task_id=?', (change_id, identity), '变更记录不存在')[0])
            if change['status'] == 'requires_followup':
                service.fail(409, '货物已模拟出库，请填写人工处理记录，不可直接改写原订单')
            if change['status'] != 'pending':
                service.fail(409, '这项变更已处理，请刷新查看记录')
            packing._version(task, data)
            if task['revision'] != change['base_revision'] or task['version'] != change['task_version']:
                service.fail(409, '任务已变化，请撤销旧变更并重新核对')
            if data.get('confirmed') is not True:
                service.fail(422, '请确认已核对变更前后内容和受影响的标签')
            packing._editable(task)
            save_revision(c, task, change['reason'])
            kind = change['change_type']
            if kind == 'revise':
                replacement = packing._normalize(c, change['proposed'])
                packing._invalidate(c, task)
                task.update(replacement)
                from . import packing_sources
                packing_sources.bind_task(c, user, task, change['proposed'])
            elif kind == 'layout':
                packing._invalidate(c, task)
                task['layout'] = layout(change['proposed'])
                # Existing source binding belongs to the same business values, on a new print revision.
                from . import packing_sources
                packing_sources.bind_task(c, user, task, {**task, 'intake_id': task.get('intake_snapshot', {}).get('intake_id'),
                                                         'intake_version': task.get('intake_snapshot', {}).get('intake_version')})
            else:
                packing._invalidate(c, task)
                task['stage'] = 'cancelled'
            task['version'] += 1
            packing._history(task, user, '已确认订单变更，原审核及旧标签作废', change['reason'])
            packing._save(c, task)
            change.update(status='confirmed', version=change['version'] + 1, confirmed_at=db.now(), confirmed_by=user['name'], result_revision=task['revision'])
            c.execute('UPDATE packing_changes SET data=? WHERE id=?', (service.js(change), change_id))
            # Other previews from the old revision cannot later overwrite this choice.
            for row in c.execute('SELECT id,data FROM packing_changes WHERE task_id=? AND id<>?', (identity, change_id)).fetchall():
                other = json.loads(row['data'])
                if other['status'] == 'pending':
                    other.update(status='superseded', message='已有另一项变更生效，请重新核对')
                    c.execute('UPDATE packing_changes SET data=? WHERE id=?', (service.js(other), row['id']))
            service.audit(c, user, 'packing_change_confirmed', identity, None, {'change_id': change_id, 'revision': task['revision']})
            return {'task': task, 'change': change}
        result = packing._operation(c, user, 'packing_change_confirm:' + change_id, data, action)
        return {**result, 'change': display_change(result['change'])}


def resolve_change(user, identity, change_id, data):
    packing._staff(user)
    with db.transaction() as c:
        def action():
            change = json.loads(service.one(c, 'SELECT data FROM packing_changes WHERE id=? AND task_id=?', (change_id, identity), '变更记录不存在')[0])
            if change['status'] not in ('pending', 'requires_followup'):
                service.fail(409, '这项变更已处理，请刷新记录')
            resolution = packing._text(data.get('resolution'), '人工处理记录', 1000)
            if not resolution:
                service.fail(422, '请填写处理决定、核实依据或撤销本次变更的原因')
            change.update(status='withdrawn' if change['status'] == 'pending' else 'resolved', resolution=resolution,
                          resolved_at=db.now(), resolved_by=user['name'], version=change['version'] + 1)
            change['history'].append({'at': db.now(), 'operator': user['name'], 'note': resolution})
            c.execute('UPDATE packing_changes SET data=? WHERE id=?', (service.js(change), change_id))
            service.audit(c, user, 'packing_change_followup', identity, None, {'change_id': change_id, 'resolution': resolution})
            return change
        return display_change(packing._operation(c, user, 'packing_change_resolve:' + change_id, data, action))


def revisions(user, identity):
    packing._staff(user)
    with db.connect() as c:
        packing._get(c, identity)
        return [{'revision': r['revision'], 'reason': r['reason'], 'created_at': r['created_at'], 'task': json.loads(r['data'])}
                for r in c.execute('SELECT * FROM packing_revisions WHERE task_id=? ORDER BY revision DESC', (identity,))]


def build_router(user_dependency):
    router = APIRouter(prefix='/api/packing')
    @router.get('/tasks/{identity}/changes')
    def listing(identity: str, u=Depends(user_dependency)):
        return list_changes(u, identity)
    @router.post('/tasks/{identity}/changes/preview')
    def preview_route(identity: str, data: dict=Body(...), u=Depends(user_dependency)):
        return preview_change(u, identity, data)
    @router.post('/tasks/{identity}/changes/{change_id}/confirm')
    def confirm_route(identity: str, change_id: str, data: dict=Body(...), u=Depends(user_dependency)):
        return confirm_change(u, identity, change_id, data)
    @router.post('/tasks/{identity}/changes/{change_id}/resolve')
    def resolve_route(identity: str, change_id: str, data: dict=Body(...), u=Depends(user_dependency)):
        return resolve_change(u, identity, change_id, data)
    @router.get('/tasks/{identity}/revisions')
    def revisions_route(identity: str, u=Depends(user_dependency)):
        return revisions(u, identity)
    return router
