"""Repeatable local comparison using the same review and scan rules as staff."""
import copy
import hashlib
import json
import threading
from fastapi import APIRouter, Body, Depends, HTTPException
from . import db, packing, packing_workflow, service

_lock = threading.Lock()


def _form(quantity=25):
    return {'supplier': '日常好物演示店', 'destination': '上海演示收货仓', 'ship_date': '2026-10-10',
            'platform_order': 'DEMO-PO-001', 'note': '演示资料。25件、每箱12件；通知改为37件时须重新审核。', 'demo': True,
            'lines': [{'sku_id': 'M1-WH', 'quantity': quantity, 'per_carton': 12, 'batch': 'DEMO-202610', 'product_barcode': '0001234567890'}]}


def compare(user, data):
    packing._staff(user)
    request_id = packing._text(data.get('request_id'), '演示提交凭证', 200)
    if not request_id:
        service.fail(422, '请重新点击加载演示')
    operation = 'packing_demo_compare'
    fingerprint = packing._fingerprint(data)
    with _lock:
        with db.connect() as c:
            row = c.execute('SELECT fingerprint,data FROM packing_operations WHERE actor_id=? AND operation=? AND request_id=?', (user['id'], operation, request_id)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    service.fail(409, '演示请求已变化，请重新加载')
                return json.loads(row['data'])
        operation_key = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        def perform(task, name, **values):
            return packing.task_action(user, task['id'], {'version': task['version'], 'request_id': f'{operation_key}:{task["id"]}:{name}', 'action': name, **values})
        def prepare(name):
            first = packing.create_task(user, {**_form(), 'platform_order': f'DEMO-PO-{name}', 'request_id': f'{operation_key}:{name}:create'})
            task = packing.get_task(user, first['id'])
            if task['revision'] > 1 or task['stage'] in ('dispatched', 'archived'):
                return task
            if task['stage'] != 'approved' and any(box['actual_quantity'] != box['planned_quantity'] for box in task['boxes']):
                task = perform(task, 'actual', boxes=[{**box, 'actual_quantity': box['planned_quantity'], 'net_weight': 3.0 if box['planned_quantity'] == 12 else .25,
                                                     'gross_weight': 3.3 if box['planned_quantity'] == 12 else .5} for box in task['boxes']],
                               materials=[{**m, 'actual_quantity': m['planned_quantity']} for m in task.get('materials', [])])
            if task['stage'] != 'approved':
                draft = packing.create_document(user, task['id'], {'request_id': f'{operation_key}:{name}:draft:{task["version"]}', 'version': task['version'], 'mode': 'draft'})
                task = perform(draft['task'], 'approve', confirmed=True, preview_document_id=draft['document']['id'])
            if not all(box.get('code') for box in task['boxes']):
                task = packing.create_document(user, task['id'], {'request_id': f'{operation_key}:{name}:formal', 'version': task['version'], 'mode': 'formal'})['task']
            return task
        normal = prepare('NORMAL')
        if normal['stage'] == 'approved':
            if any(not box['labeled_at'] for box in normal['boxes']):
                normal = perform(normal, 'label')
            for box in list(normal['boxes']):
                if not box['scanned_at']:
                    normal = packing.scan(user, {'task_id': normal['id'], 'code': box['code'], 'request_id': f'{operation_key}:{box["id"]}:scan'})['task']
            normal = perform(normal, 'dispatch')
        if normal['stage'] == 'dispatched':
            normal = perform(normal, 'archive')
        abnormal = prepare('CHANGE')
        if abnormal['revision'] == 1:
            proposed = copy.deepcopy(_form(37))
            proposed['platform_order'] = abnormal['platform_order']
            change = packing_workflow.preview_change(user, abnormal['id'], {'version': abnormal['version'], 'request_id': f'{operation_key}:change:preview',
                        'change_type': 'revise', 'reason': '演示客户通知：总量由25件改为37件，每箱12件不变',
                        'source_note': '模拟客户通知：请将这次发货数量改成37件。', 'proposed': proposed})
            result = packing_workflow.confirm_change(user, abnormal['id'], change['id'], {'version': abnormal['version'], 'request_id': f'{operation_key}:change:confirm', 'confirmed': True})
            abnormal = result['task']
        with db.connect() as c:
            old_code = c.execute('SELECT code FROM packing_labels WHERE task_id=? AND active=0 ORDER BY rowid LIMIT 1', (abnormal['id'],)).fetchone()[0]
        try:
            packing.scan(user, {'code': old_code, 'task_id': abnormal['id'], 'request_id': f'{operation_key}:old-scan'})
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            rejection = exc.detail
        else:
            raise RuntimeError('Old demo label unexpectedly accepted')
        value = {'normal': packing.get_task(user, normal['id']), 'abnormal': packing.get_task(user, abnormal['id']),
                 'old_code': old_code, 'old_code_rejection': rejection,
                 'comparison': [{'label': '客户要求', 'normal': '25件，每箱12件', 'abnormal': '打印后改成37件'},
                                {'label': '装箱计划', 'normal': '12＋12＋1，共3箱', 'abnormal': '12＋12＋12＋1，共4箱'},
                                {'label': '审核结果', 'normal': '已核对草稿、审核并逐箱核验', 'abnormal': '原审核作废，需要重新记录实装及审核'},
                                {'label': '旧标签', 'normal': '当前标签已用于本地核验', 'abnormal': rejection},
                                {'label': '出库状态', 'normal': '已模拟出库并归档', 'abnormal': '仍待核对，不可出库'}],
                 'notice': '全部为演示操作，使用真实本地校验流程；未调用AI、未扣库存或创建真实物流单。'}
        with db.transaction() as c:
            c.execute('INSERT INTO packing_operations VALUES(?,?,?,?,?)', (user['id'], operation, request_id, fingerprint, service.js(value)))
        return value


def build_router(user_dependency):
    router = APIRouter(prefix='/api/packing')
    @router.post('/demo/compare')
    def compare_route(data: dict=Body(...), u=Depends(user_dependency)):
        return compare(u, data)
    return router
