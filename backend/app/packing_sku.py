"""Packing-only SKU controls; retail stock and historical orders stay untouched."""
import json
import re

from . import db, service


def init_schema(c):
    c.execute('CREATE TABLE IF NOT EXISTS packing_sku_records(sku_id TEXT PRIMARY KEY,version INTEGER NOT NULL,data TEXT NOT NULL)')


def barcode(value):
    if not isinstance(value, str) or len(value) > 80 or value and not re.fullmatch(r'[\x20-\x7e]{1,80}', value):
        service.fail(422, '商品条码请按文本填写英文字母、数字或符号，并保留前导零')
    return value.strip()


def current(c, sku_id):
    sku = service.one(c, 'SELECT id,product_id,label FROM skus WHERE id=?', (sku_id,), '内部 SKU 不存在，请重新选择')
    row = c.execute('SELECT version,data FROM packing_sku_records WHERE sku_id=?', (sku_id,)).fetchone()
    saved = json.loads(row['data']) if row else {}
    return {'sku_id': sku['id'], 'product_id': sku['product_id'], 'specification': sku['label'],
            'version': row['version'] if row else 0,
            'product_barcode': saved.get('product_barcode', ''), 'packing_enabled': saved.get('packing_enabled', True)}


def for_product(c, product_id):
    return [current(c, row['id']) for row in c.execute('SELECT id FROM skus WHERE product_id=? ORDER BY rowid', (product_id,))]


def proposed(current_rows, values, reason):
    if values is None:
        return current_rows  # Older packaging clients preserve current SKU controls.
    if not isinstance(values, list) or len(values) != len(current_rows):
        service.fail(422, '请逐个核对这个商品的全部 SKU 条码与装箱启停')
    existing = {row['sku_id']: row for row in current_rows}
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or value.get('sku_id') not in existing or value['sku_id'] in seen:
            service.fail(422, 'SKU 不属于当前商品或存在重复，请重新选择商品')
        seen.add(value['sku_id'])
        if type(value.get('packing_enabled')) is not bool:
            service.fail(422, '请明确选择每个 SKU 是否启用装箱')
        before = existing[value['sku_id']]
        after = {**before, 'product_barcode': barcode(value.get('product_barcode')), 'packing_enabled': value['packing_enabled']}
        if any(before[k] != after[k] for k in ('product_barcode', 'packing_enabled')) and not reason:
            service.fail(422, '修改 SKU 条码或装箱启停须填写核对依据与原因')
        result.append(after)
    return sorted(result, key=lambda row: list(existing).index(row['sku_id']))


def save(c, user, values, reason):
    for value in values:
        before = current(c, value['sku_id'])
        if all(before[k] == value[k] for k in ('product_barcode', 'packing_enabled')):
            continue
        after = {**value, 'version': before['version'] + 1, 'operator': user['name'], 'actor_id': user['id'],
                 'updated_at': db.now(), 'reason': reason}
        c.execute('INSERT INTO packing_sku_records VALUES (?,?,?) ON CONFLICT(sku_id) DO UPDATE SET version=excluded.version,data=excluded.data',
                  (value['sku_id'], after['version'], service.js(after)))
        service.audit(c, user, 'packing_sku_save', value['sku_id'], before, after)


def issues(state, used_barcode):
    return {'disabled': not state['packing_enabled'],
            'barcode_conflict': bool(used_barcode and state['product_barcode'] and used_barcode != state['product_barcode'])}


def confirmation_valid(state, used_barcode, confirmation):
    flags = issues(state, used_barcode)
    if not any(flags.values()):
        return True
    return (isinstance(confirmation, dict) and type(confirmation.get('version')) is int and confirmation['version'] == state['version']
            and isinstance(confirmation.get('reason'), str) and 0 < len(confirmation['reason'].strip()) <= 500
            and (not flags['disabled'] or confirmation.get('disabled_confirmed') is True)
            and (not flags['barcode_conflict'] or confirmation.get('barcode_conflict_confirmed') is True))


def review(state, used_barcode, confirmation=None):
    flags = issues(state, used_barcode)
    if not confirmation_valid(state, used_barcode, confirmation):
        labels = ('停用 SKU 的本单例外' if flags['disabled'] else '') + ('、商品条码冲突' if flags['barcode_conflict'] else '')
        service.fail(422, f'请重新人工确认{labels.lstrip("、")}，填写本单核对原因；本单例外不能自动复用')
    if state['version'] == 0 and not any(flags.values()):
        return None  # Historical records need no rewrite for the compatible default.
    return {**state, 'used_barcode': used_barcode, **flags,
            'disabled_confirmed': bool(flags['disabled']), 'barcode_conflict_confirmed': bool(flags['barcode_conflict']),
            'reason': confirmation['reason'].strip() if any(flags.values()) else ''}


def validate_task(c, task):
    if task.get('stage') in ('dispatched', 'archived', 'cancelled'):
        return
    for index, line in enumerate(task.get('lines', []), 1):
        state = current(c, line['sku_id'])
        used = line.get('product_barcode', '')
        flags = issues(state, used)
        if not any(flags.values()):
            continue
        saved = line.get('sku_safety_review')
        if (not confirmation_valid(state, used, saved) or saved.get('used_barcode') != used):
            service.fail(409, f'第 {index} 行 SKU 装箱状态或条码已变化，请登记资料变更并重新核对本单例外、预览和审核')


def task_checks(c, task):
    result = []
    for index, line in enumerate(task.get('lines', []), 1):
        if not c.execute('SELECT 1 FROM skus WHERE id=?', (line['sku_id'],)).fetchone():
            result.append({'sku_id': line['sku_id'], 'line_index': index - 1, 'missing': True, 'requires_recheck': True})
            continue
        state = current(c, line['sku_id'])
        flags = issues(state, line.get('product_barcode', ''))
        saved = line.get('sku_safety_review')
        result.append({**state, **flags, 'line_index': index - 1,
                       'requires_recheck': any(flags.values()) and (not confirmation_valid(state, line.get('product_barcode', ''), saved)
                                           or saved.get('used_barcode') != line.get('product_barcode', ''))})
    return result
