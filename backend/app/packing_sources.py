"""Employee source understanding, evidence review and verified SKU mappings.

Source candidates never approve a shipment. Human review and a server-owned
binding tie a complete, versioned source snapshot to the actual packing lines.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
from datetime import date

from fastapi import APIRouter, Body, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from . import db, service, model_usage, packing_sku, packing_sources_ai as source_ai
from . import packing_sources_parsers as parsers

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS packing_intakes(id TEXT PRIMARY KEY,data TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS packing_intake_revisions(intake_id TEXT NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(intake_id,version))',
    'CREATE TABLE IF NOT EXISTS packing_source_operations(actor_id TEXT NOT NULL,operation TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(actor_id,operation,request_id))',
    'CREATE TABLE IF NOT EXISTS packing_sku_mappings(id TEXT PRIMARY KEY,customer TEXT NOT NULL,external_code TEXT NOT NULL,specification TEXT NOT NULL,sku_id TEXT NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL,UNIQUE(customer,external_code,specification))',
    'CREATE TABLE IF NOT EXISTS packing_sku_mapping_revisions(mapping_id TEXT NOT NULL,version INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(mapping_id,version))',
    'CREATE TABLE IF NOT EXISTS packing_source_bindings(task_id TEXT NOT NULL,revision INTEGER NOT NULL,intake_id TEXT NOT NULL,intake_version INTEGER NOT NULL,digest TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(task_id,revision))',
)

TOP_FIELDS = ('supplier', 'destination', 'ship_date', 'platform_order', 'note')
LINE_FIELDS = ('external_code', 'product_name', 'specification', 'quantity', 'per_carton', 'batch', 'produced_at',
               'expires_at', 'product_barcode', 'kit_note', 'serial_numbers', 'require_batch', 'require_expiry', 'require_serials')
RULE_FIELDS = ('required_fields', 'label_width_mm', 'label_height_mm')
REQUIREMENTS = ('require_batch', 'require_expiry', 'require_serials')
PLAN_LINE_FIELDS = ('sku_id', 'external_code', 'quantity', 'per_carton', 'batch', 'produced_at', 'expires_at', 'product_barcode',
                    'kit_note', 'serial_numbers', *REQUIREMENTS)
CORE_REQUIRED = ['supplier', 'destination', 'ship_date', 'lines.external_code', 'lines.specification', 'lines.quantity', 'lines.per_carton']
ALLOWED_REQUIRED = set(TOP_FIELDS) | {'lines.' + field for field in LINE_FIELDS if field not in REQUIREMENTS}
TITLES = {'supplier': '发货单位', 'destination': '收货仓库', 'ship_date': '计划发货日期', 'platform_order': '平台单号',
          'note': '发货备注', 'external_code': '客户商品编码', 'product_name': '商品名称', 'specification': '原资料商品规格',
          'quantity': '发货数量', 'per_carton': '每箱数量', 'batch': '批次', 'produced_at': '生产日期', 'expires_at': '到期日期',
          'product_barcode': '商品条码', 'kit_note': '套装及配件说明', 'serial_numbers': '序列号',
          'require_batch': '批次追溯要求', 'require_expiry': '效期要求', 'require_serials': '逐件序列号要求',
          'required_fields': '箱唛规则必填字段', 'label_width_mm': '箱唛标签宽度（毫米）', 'label_height_mm': '箱唛标签高度（毫米）'}


def init_schema(c):
    # Do not executescript here: the caller may be inside a migration transaction.
    for statement in SCHEMA:
        c.execute(statement)
    c.execute('CREATE INDEX IF NOT EXISTS packing_source_bindings_intake ON packing_source_bindings(intake_id)')


def recover_pending(c):
    """Startup-only recovery; never call while a provider extraction is active."""
    init_schema(c)
    pending = [json.loads(row[0]) for row in c.execute('SELECT data FROM packing_intakes')]
    recovered = 0
    for intake in pending:
        if intake.get('status') != 'extracting':
            continue
        _touch(intake, {'id': 'staff', 'name': '系统恢复'}, '服务重启中断 AI 提取，保留原资料', '服务进程已重启，未完成的模型结果不能作为已确认资料')
        intake.pop('extraction_request', None)
        intake['status'] = 'extraction_failed'
        intake['report'] = {'title': '资料核对报告', 'model_status': 'interrupted',
                            'messages': ['服务重启中断了 AI 提取。原资料仍已保留，请使用新的提交凭证重新提取并人工核对。']}
        _save(c, intake)
        recovered += 1
    return recovered


def _staff(user):
    if not isinstance(user, dict) or user.get('role') != 'staff':
        service.fail(403, '请由员工进入箱唛资料核对工作台')


def _text(value, title, maximum=200, required=False):
    if not isinstance(value, str) or len(value) > maximum or any(ord(ch) < 32 and ch not in '\n\t\r' for ch in value):
        service.fail(422, f'{title}格式不正确或内容过长')
    result = value.strip()
    if required and not result:
        service.fail(422, f'请填写{title}')
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _get(c, identity):
    identity = _text(identity, '资料编号', 100, True)
    return json.loads(service.one(c, 'SELECT data FROM packing_intakes WHERE id=?', (identity,), '箱唛资料不存在')[0])


def _check_version(intake, data):
    if type(data.get('version')) is not int or data['version'] != intake['version']:
        service.fail(409, '资料已更新，请重新打开并核对最新版本')
    if intake.get('status') == 'extracting':
        service.fail(409, '资料正在提取，请等待完成后再修改')


def _reason(data):
    return _text(data.get('reason', ''), '本次核对或修改原因', 500, True)


def _role(value):
    if value not in ('shipment', 'rule', 'packaging'):
        service.fail(422, '请选择发货资料、包装资料或规则资料')
    return value


def _operation_key(c, user, operation, data):
    key = _text(data.get('request_id', ''), '提交凭证', 200, True)
    digest = _hash(data)
    row = c.execute('SELECT fingerprint,data FROM packing_source_operations WHERE actor_id=? AND operation=? AND request_id=?',
                    (user['id'], operation, key)).fetchone()
    if row and row['fingerprint'] != digest:
        service.fail(409, '这次提交的内容已变化，请重新核对后提交')
    return key, digest, json.loads(row['data']) if row else None


def _record_operation(c, user, operation, key, digest, result):
    c.execute('INSERT INTO packing_source_operations VALUES (?,?,?,?,?) ON CONFLICT(actor_id,operation,request_id) DO UPDATE SET data=excluded.data',
              (user['id'], operation, key, digest, service.js(result)))


def _operation(c, user, operation, data, callback):
    key, digest, previous = _operation_key(c, user, operation, data)
    if previous is not None:
        return previous
    result = callback()
    _record_operation(c, user, operation, key, digest, result)
    return result


def _field(name, line=None):
    index = line['index'] if line else None
    path = f"lines.{line['id']}.{name}" if line else 'rules.' + name if name in RULE_FIELDS else name
    return {'id': db.uid('field'), 'name': name, 'path': path, 'line_id': line['id'] if line else None,
            'title': (f'第 {index} 行' if index else '') + TITLES[name], 'value': None, 'origin': 'source',
            'confirmed': False, 'candidates': [], 'citations': [], 'confirmation': None,
            'rejected_candidates': [], 'active_rejections': []}


def _lookup(intake, name, line_id=None):
    return next(field for field in intake['fields'] if field['name'] == name and field['line_id'] == line_id)


def _blank(value):
    return value is None or value == '' or value == []


def _value(name, value):
    if value is None:
        return None
    if name in ('quantity', 'per_carton', 'label_width_mm', 'label_height_mm'):
        maximum = 100000 if name == 'quantity' else 10000 if name == 'per_carton' else 1000
        if type(value) is not int or not 1 <= value <= maximum:
            service.fail(422, f'{TITLES[name]}必须为 1 至 {maximum} 的整数')
        return value
    if name in REQUIREMENTS:
        if type(value) is not bool:
            service.fail(422, f'{TITLES[name]}请填写明确的勾选要求')
        return value
    if name == 'required_fields':
        if not isinstance(value, list) or len(value) > len(ALLOWED_REQUIRED) or any(not isinstance(item, str) or item not in ALLOWED_REQUIRED for item in value):
            service.fail(422, '规则必填字段含不支持的字段，请逐项核对')
        return list(dict.fromkeys(value))
    if name == 'serial_numbers':
        if not isinstance(value, list) or len(value) > 100000:
            service.fail(422, '序列号请逐个填写为字符串数组')
        result = [_text(item, '序列号', 80, True) for item in value]
        if len(set(result)) != len(result):
            service.fail(422, '序列号不能重复')
        return result
    result = _text(value, TITLES[name], 500 if name in ('note', 'kit_note') else 200)
    if name in ('ship_date', 'produced_at', 'expires_at') and result:
        try:
            if date.fromisoformat(result).isoformat() != result:
                raise ValueError()
        except ValueError:
            service.fail(422, f'{TITLES[name]}必须填写完整 YYYY-MM-DD 日期，不能猜测年月日')
    if name in ('external_code', 'product_barcode') and result and any(char.isspace() for char in result):
        service.fail(422, f'{TITLES[name]}含空白，请按原编码逐字符核对')
    return result


def _references(intake, citations, authorized_images=None):
    if not isinstance(citations, list) or not 1 <= len(citations) <= 12:
        service.fail(422, 'AI 候选缺少可核对的原资料引用')
    sources = {source['id']: source for source in intake['sources']}
    result = []
    for citation in citations:
        if not isinstance(citation, dict):
            service.fail(422, '字段出处格式不正确')
        source = sources.get(citation.get('source_id'))
        quote = citation.get('quote')
        if not source or not isinstance(quote, str) or not quote.strip() or len(quote) > 6000:
            service.fail(422, '字段出处缺少实际原资料或引文')
        if citation.get('chunk_id'):
            chunk = next((item for item in source['chunks'] if item['id'] == citation['chunk_id']), None)
            if not chunk or quote not in chunk['text']:
                service.fail(422, '字段引文与原资料分块不一致，不能作为核对依据')
            result.append({'source_id': source['id'], 'chunk_id': chunk['id'], 'quote': quote,
                           'locator': chunk['locator'], 'verification': '原文逐字可核对'})
        elif citation.get('image_id'):
            page = next((item for item in source['pages'] if item['image_id'] == citation['image_id']), None)
            if not page or authorized_images is not None and page['image_id'] not in authorized_images:
                service.fail(422, '字段引用的图片未实际提供给本次 AI，不能声称已经识别')
            result.append({'source_id': source['id'], 'image_id': page['image_id'], 'quote': quote,
                           'locator': {'page': page['page']}, 'verification': 'AI 图像转录，待人工逐页核对'})
        else:
            service.fail(422, '字段出处缺少分块编号或实际图片编号')
    return result


def _quantity_evidence(intake, name, value, citations):
    if name not in ('quantity', 'per_carton'):
        return
    sources = {source['id']: source for source in intake['sources']}
    labels = (r'每箱(?:数量|件数|装|放)?|装箱数|箱内数量|units?\s*per\s*carton|qty\s*per\s*carton|carton\s*qty|per_carton' if name == 'per_carton' else
              r'发货(?:数量|件数|总量)?|出货数量|配送数量|送货数量|总发货量|总共|总计|共计|合计数量|shipment\s*(?:qty|quantity)|dispatch\s*(?:qty|quantity)|quantity|qty|数量')
    changes = r'(?:应改(?:成|为)|改(?:成|为)|更正(?:为|成)|调整(?:为|至)|修改(?:为|成)|更改(?:为|成)|更新为|变更为|确认(?:为)?|现为|应为|为)'
    old_to_new = r'(?:从|由)\s*\d+\s*(?:件|套|个|箱|盒|台)?\s*' + changes
    qualifier = r'(?:' + old_to_new + '|' + changes + r')?'
    for citation in citations:
        source = sources[citation['source_id']]
        if source['role'] != 'shipment':
            continue
        # Check the full original row so a bare number cannot lose its header.
        text = citation['quote']
        if citation.get('chunk_id'):
            text = next(chunk['text'] for chunk in source['chunks'] if chunk['id'] == citation['chunk_id'])
        for match in re.finditer(r'(?:' + labels + r')\s*[:=：]?\s*' + qualifier + r'\s*' + str(value) + r'(?![\d.])', text, re.I):
            prefix = text[max(0, match.start() - 12):match.start()]
            strong = re.match(r'发货|出货|配送|送货|总发货|每箱|装箱|箱内|shipment|dispatch|units?', match.group(), re.I)
            if strong or not re.search(r'入库|采购|库存|金额|单价|收货|\bPO\b|inbound|stock|amount|price|received', prefix, re.I):
                return
    service.fail(422, f'{TITLES[name]}出处没有明确发货或每箱语义，不能把入库、PO 或金额当数量')


def _scalar_evidence(name, value, citations):
    """Protect identifiers against invented values and lost leading zeroes."""
    if name in ('external_code', 'product_barcode', 'serial_numbers'):
        values = value if isinstance(value, list) else [value]
        for item in values:
            pattern = r'(?<![A-Za-z0-9_-])' + re.escape(item) + r'(?![A-Za-z0-9_-])'
            if not any(re.search(pattern, citation['quote']) for citation in citations):
                service.fail(422, f'{TITLES[name]}与逐字原文不一致，请保留前导零和完整编号')
    elif name in ('specification', 'product_name', 'supplier', 'destination', 'platform_order'):
        if not any(value in citation['quote'] for citation in citations):
            service.fail(422, f'{TITLES[name]}值不在所引原文中，不能采用猜测或改写')
    elif name in ('ship_date', 'produced_at', 'expires_at'):
        year, month, day = (int(part) for part in value.split('-'))
        pattern = str(year) + r'\s*[-/年.]\s*0?' + str(month) + r'\s*[-/月.]\s*0?' + str(day) + r'(?!\d)'
        if not any(re.search(pattern, citation['quote']) for citation in citations):
            service.fail(422, f'{TITLES[name]}出处没有对应的完整年月日，不能补猜日期')


def _reject(field, candidate, message):
    entry = {'id': db.uid('rejected'), 'value': copy.deepcopy(candidate.get('value')) if isinstance(candidate, dict) else candidate,
             'citations': copy.deepcopy(candidate.get('citations', [])) if isinstance(candidate, dict) else [],
             'error': message, 'at': db.now(), 'origin': 'ai'}
    # Provider output is already bounded. Preserve its rejected value as data,
    # never promote it to an optional blank/default field or execute its text.
    field.setdefault('rejected_candidates', []).append(entry)
    field.setdefault('active_rejections', []).append(entry['id'])
    field.update(confirmed=False, confirmation=None)


def _append_candidate(field, value, citations, origin, reason=''):
    key = _hash({'value': value, 'citations': citations, 'origin': origin})
    if any(candidate.get('fingerprint') == key for candidate in field['candidates']):
        return
    if len(field['candidates']) >= 100:
        service.fail(422, '字段候选过多，请拆分资料重新核对')
    field['candidates'].append({'id': db.uid('candidate'), 'value': value, 'origin': origin, 'citations': citations,
                                'reason': reason, 'created_at': db.now(), 'fingerprint': key})
    if field.get('confirmed') and value != field['value']:
        field.update(confirmed=False, confirmation=None)
    _refresh_field(field)


def _refresh_field(field):
    distinct = {_hash(candidate['value']): candidate for candidate in field['candidates']}
    if field.get('confirmed') and field.get('confirmation'):
        field['origin'] = 'human'
        field['issue'] = ''
    elif len(distinct) == 1:
        candidate = next(iter(distinct.values()))
        field.update(value=candidate['value'], origin=candidate['origin'], citations=candidate['citations'], issue='待人工核对')
    elif distinct:
        field.update(value=None, origin='ai', citations=[], issue='资料存在冲突，请明确选择或补录并说明原因')
    else:
        field.update(value=None, origin='source', citations=[], issue='原资料未明确，需补充核对')
    field['conflict'] = len(distinct) > 1 and not field.get('confirmed')
    if field.get('active_rejections'):
        field['issue'] = '存在未处理的无效候选，须人工更正并说明原因'


def _new_line(intake):
    if len(intake['lines']) >= 50:
        service.fail(422, '一组资料最多 50 行商品，请拆分后核对')
    line = {'id': db.uid('line'), 'index': len(intake['lines']) + 1, 'sku_id': None, 'selection': None,
            'mapping_status': 'needs_confirmation', 'candidates': [], 'field_ids': []}
    for name in LINE_FIELDS:
        field = _field(name, line)
        intake['fields'].append(field)
        line['field_ids'].append(field['id'])
    intake['lines'].append(line)
    return line


def _raw_value(value):
    return value.get('value') if isinstance(value, dict) else None


def _match_line(intake, raw):
    code, spec, batch, name = (_raw_value(raw.get(key)) for key in ('external_code', 'specification', 'batch', 'product_name'))
    for line in intake['lines']:
        current_code = _lookup(intake, 'external_code', line['id'])['value']
        current_batch = _lookup(intake, 'batch', line['id'])['value']
        code_values = [candidate['value'] for candidate in _lookup(intake, 'external_code', line['id'])['candidates']]
        if isinstance(code, str) and code and (code == current_code or code in code_values) and (not batch or not current_batch or batch == current_batch):
            return line
        if not code and name and spec and name == _lookup(intake, 'product_name', line['id'])['value'] and spec == _lookup(intake, 'specification', line['id'])['value'] and (batch or '') == (current_batch or ''):
            return line
    return _new_line(intake)


def _ingest_field(intake, field, obj, images, errors):
    if obj is None:
        return
    if not isinstance(obj, dict):
        message = field['title'] + '未提供字段出处，候选未采用'
        _reject(field, obj, message)
        errors.append(message)
        return
    alternatives = obj.get('alternatives', [])
    if not isinstance(alternatives, list) or len(alternatives) > 20:
        message = field['title'] + '候选结构不正确，候选未采用'
        _reject(field, obj, message)
        errors.append(message)
        return
    for candidate in [obj, *alternatives]:
        if not isinstance(candidate, dict) or candidate.get('value') is None:
            continue
        try:
            value = _value(field['name'], candidate['value'])
            if _blank(value):
                continue
            citations = _references(intake, candidate.get('citations'), images)
            _quantity_evidence(intake, field['name'], value, citations)
            _scalar_evidence(field['name'], value, citations)
            if field['name'] in ('external_code', 'specification', 'quantity', 'per_carton') and all(next(source for source in intake['sources'] if source['id'] == citation['source_id'])['role'] != 'shipment' for citation in citations):
                service.fail(422, '发货关键字段不能只引用规则或包装资料')
            _append_candidate(field, value, citations, 'ai')
        except Exception as exc:
            from fastapi import HTTPException
            if not isinstance(exc, HTTPException):
                raise
            message = field['title'] + '：' + str(exc.detail)
            _reject(field, candidate, message)
            errors.append(message)


def _apply_extraction(intake, raw, images):
    errors = []
    top = raw.get('fields', {})
    if not isinstance(top, dict):
        service.fail(422, 'AI 返回的顶层字段结构不正确，资料保留草稿')
    for name in TOP_FIELDS:
        _ingest_field(intake, _lookup(intake, name), top.get(name), images, errors)
    for item in raw.get('lines', []):
        if not isinstance(item, dict):
            service.fail(422, 'AI 商品行结构不完整，资料保留草稿，请重新提取')
        if not any(isinstance(item.get(name), dict) and item[name].get('value') is not None for name in LINE_FIELDS):
            continue
        line = _match_line(intake, item)
        for name in LINE_FIELDS:
            _ingest_field(intake, _lookup(intake, name, line['id']), item.get(name), images, errors)
    rules = raw.get('rules', {})
    if isinstance(rules, dict):
        for name in RULE_FIELDS:
            _ingest_field(intake, _lookup(intake, name), rules.get(name), images, errors)
    warnings = raw.get('warnings', [])
    if isinstance(warnings, list):
        errors += [message[:500] for message in warnings[:30] if isinstance(message, str)]
    for source in intake['sources']:
        source['extracted'] = True
    intake['rules']['confirmation'] = None
    intake['report'] = {'title': '资料核对报告', 'model_status': 'succeeded',
                        'messages': ['已调用 AI 提取候选。原资料与候选均已保留；关键字段、SKU 与规则须人工核对。', *errors]}


def _catalog(c):
    result = []
    for row in c.execute('SELECT skus.id,skus.label,skus.product_id,products.data FROM skus JOIN products ON products.id=skus.product_id ORDER BY skus.rowid'):
        product = json.loads(row['data'])
        result.append({'sku_id': row['id'], 'product_id': row['product_id'], 'product_name': product['name'],
                       'specification': row['label'], 'aliases': [product.get('model', ''), *product.get('aliases', [])],
                       **packing_sku.current(c, row['id'])})
    return result


def _mapping_id(customer, code, specification):
    return 'skumap_' + _hash([customer, code, specification])


def _refresh_mapping(c, intake, line, catalog):
    code = _lookup(intake, 'external_code', line['id'])['value']
    spec = _lookup(intake, 'specification', line['id'])['value']
    name = _lookup(intake, 'product_name', line['id'])['value']
    barcode = _lookup(intake, 'product_barcode', line['id'])['value'] or ''
    fields_ok = all(not _lookup(intake, field, line['id'])['conflict'] for field in ('external_code', 'specification', 'product_barcode'))
    row = c.execute('SELECT * FROM packing_sku_mappings WHERE customer=? AND external_code=? AND specification=?',
                    (intake['customer'], code, spec)).fetchone() if isinstance(code, str) and isinstance(spec, str) and fields_ok else None
    chosen = line.get('selection')
    by_sku = {item['sku_id']: item for item in catalog}
    mapped = by_sku.get(row['sku_id']) if row else None
    selected = by_sku.get(chosen.get('sku_id')) if chosen else None
    barcode_choices = [item for item in catalog if barcode and item['product_barcode'] == barcode]
    barcode_ids = sorted(item['sku_id'] for item in barcode_choices)
    barcode_ambiguous = bool(mapped and barcode_ids and barcode_ids != [mapped['sku_id']])
    automatic_ok = mapped and not barcode_ambiguous and not any(packing_sku.issues(mapped, barcode).values())
    legacy_barcode_choice = bool(chosen and selected and selected['version'] == 0 and not barcode_ids
                                 and 'barcode_candidates' not in chosen and 'sku_version' not in chosen)
    selected_ok = (chosen and selected and fields_ok and chosen['external_code'] == code and chosen['specification'] == spec
                   and chosen['customer'] == intake['customer'] and chosen.get('product_barcode', barcode) == barcode
                   and (not barcode or legacy_barcode_choice or chosen.get('barcode_candidates') == barcode_ids and chosen.get('sku_version') == selected['version'])
                   and packing_sku.confirmation_valid(selected, barcode, chosen.get('sku_confirmation')))
    if row and automatic_ok:
        line.update(sku_id=row['sku_id'], mapping_status='confirmed_mapping', mapping_id=row['id'], mapping_version=row['version'])
    elif selected_ok:
        line.update(sku_id=chosen['sku_id'], mapping_status='human_confirmed', mapping_id=None, mapping_version=None)
    else:
        flags = packing_sku.issues(mapped or selected, barcode) if mapped or selected else {}
        status = 'specification_conflict' if not fields_ok else 'disabled_sku' if flags.get('disabled') else 'barcode_conflict' if flags.get('barcode_conflict') or barcode_ambiguous else 'needs_confirmation'
        line.update(sku_id=None, mapping_status=status, mapping_id=None, mapping_version=None)
    exact_name = [item for item in catalog if name and (name.casefold() == item['product_name'].casefold() or any(name.casefold() == alias.casefold() for alias in item['aliases'] if alias))]
    choices = [item for item in exact_name if spec == item['specification']] or exact_name
    if not choices and spec:
        choices = [item for item in catalog if spec == item['specification']]
    choices = list({item['sku_id']: item for item in [*barcode_choices, *choices]}.values())
    # A known mapping works without an external product name. New/multiple
    # candidates are never auto-selected, even if only one name seems to match.
    def candidate(item):
        result = {k: item[k] for k in ('sku_id', 'product_name', 'specification')}
        if item['version']:
            result.update(product_barcode=item['product_barcode'], packing_enabled=item['packing_enabled'], packing_version=item['version'],
                          barcode_match=bool(barcode and barcode == item['product_barcode']),
                          barcode_conflict=packing_sku.issues(item, barcode)['barcode_conflict'])
        return result
    line['candidates'] = [candidate(item) for item in choices]
    if line['sku_id'] and not any(item['sku_id'] == line['sku_id'] for item in line['candidates']):
        item = next(item for item in catalog if item['sku_id'] == line['sku_id'])
        line['candidates'].insert(0, candidate(item))


def _refresh(c, intake):
    for field in intake['fields']:
        _refresh_field(field)
    catalog = _catalog(c)
    for line in intake['lines']:
        _refresh_mapping(c, intake, line, catalog)
    required = set(CORE_REQUIRED)
    rule_field = _lookup(intake, 'required_fields')
    if isinstance(rule_field['value'], list):
        required.update(rule_field['value'])
    elif rule_field.get('conflict'):
        for candidate in rule_field['candidates']:
            required.update(candidate['value'])
    rules = intake['rules']
    rules.update(required_fields=sorted(required), label_width_mm=_lookup(intake, 'label_width_mm')['value'],
                 label_height_mm=_lookup(intake, 'label_height_mm')['value'],
                 confirmed=bool(rules.get('confirmation') and all(_lookup(intake, name)['confirmed'] for name in RULE_FIELDS)),
                 candidates=[{'field': name, **candidate} for name in RULE_FIELDS for candidate in _lookup(intake, name)['candidates']],
                 citations=[citation for name in RULE_FIELDS for citation in _lookup(intake, name)['citations']])
    missing = []
    if intake.get('report', {}).get('model_status') in ('failed', 'interrupted'):
        missing.append('本次 AI 提取未完成，资料保留草稿；请重新提取并人工核对')
    if any(not source['extracted'] for source in intake['sources']):
        missing.append('新增原资料尚未完成 AI 提取，请先提取后核对')
    if not intake['sources']:
        missing.append('请上传原资料或粘贴聊天原文')
    if not intake['lines']:
        missing.append('尚未识别发货商品行，请补齐明确的商品编码、规格和发货数量')
    for field in intake['fields']:
        if field.get('active_rejections'):
            missing.append('请处理' + field['title'] + '的无效候选，并说明人工核对或更正原因')
        if field['name'] in RULE_FIELDS:
            continue
        mandatory = ('lines.' + field['name'] if field['line_id'] else field['name']) in required
        needs_confirmation = mandatory or bool(field['candidates'])
        if mandatory and _blank(field['value']):
            missing.append('请补齐' + field['title'])
        elif needs_confirmation and not field['confirmed']:
            missing.append('请人工核对' + field['title'])
    for line in intake['lines']:
        if not line['sku_id']:
            missing.append(f"第 {line['index']} 行客户编码及规格尚未确认对应内部 SKU")
        lid = line['id']
        quantity = _lookup(intake, 'quantity', lid)['value']
        serials = _lookup(intake, 'serial_numbers', lid)['value']
        if serials and quantity is not None and len(serials) != quantity:
            missing.append(f"第 {line['index']} 行序列号数量与发货数量不一致")
        if _lookup(intake, 'require_batch', lid)['value'] and _blank(_lookup(intake, 'batch', lid)['value']):
            missing.append(f"第 {line['index']} 行已要求批次追溯，须补齐批次")
        if _lookup(intake, 'require_expiry', lid)['value'] and any(_blank(_lookup(intake, name, lid)['value']) for name in ('produced_at', 'expires_at')):
            missing.append(f"第 {line['index']} 行已要求效期管理，须补齐生产与到期日期")
        if _lookup(intake, 'require_serials', lid)['value'] and (not serials or len(serials) != quantity):
            missing.append(f"第 {line['index']} 行已要求逐件追溯，须补齐全部序列号")
        produced, expires = (_lookup(intake, name, lid)['value'] for name in ('produced_at', 'expires_at'))
        if produced and expires and produced > expires:
            missing.append(f"第 {line['index']} 行到期日期早于生产日期")
    if not rules['confirmed']:
        missing.append('箱唛规则必填项和标签尺寸尚未人工确认')
    width, height = rules['label_width_mm'], rules['label_height_mm']
    if rules['confirmed'] and (type(width) is not int or type(height) is not int or not 100 <= width <= 200 or not 150 <= height <= 270):
        missing.append('已确认尺寸不适用于当前箱唛模板，请核对尺寸')
    total_boxes = sum(math.ceil(_lookup(intake, 'quantity', line['id'])['value'] / _lookup(intake, 'per_carton', line['id'])['value'])
                      for line in intake['lines'] if _lookup(intake, 'quantity', line['id'])['value'] and _lookup(intake, 'per_carton', line['id'])['value'])
    if total_boxes > 300:
        missing.append('分箱超过 300 箱，请拆分为多个资料任务')
    intake['conflicts'] = [{'field_id': field['id'], 'title': field['title'], 'candidates': field['candidates']} for field in intake['fields'] if field['conflict']]
    intake['missing'] = list(dict.fromkeys(missing))
    intake['ready'] = not intake['missing'] and not intake['conflicts'] and intake.get('status') != 'extracting'
    if intake.get('status') not in ('extracting', 'extraction_failed'):
        intake['status'] = 'ready' if intake['ready'] else 'needs_review'
    binding = c.execute('SELECT task_id FROM packing_source_bindings WHERE intake_id=? ORDER BY revision DESC LIMIT 1', (intake['id'],)).fetchone()
    intake['bound_task_id'] = binding['task_id'] if binding else None
    return intake


def _touch(intake, user, action, reason):
    intake['version'] += 1
    intake['updated_at'] = db.now()
    intake['status'] = 'needs_review'
    intake['history'].append({'version': intake['version'], 'at': intake['updated_at'], 'operator': user['name'], 'actor_id': user['id'],
                              'action': action, 'reason': reason})


def _save(c, intake):
    _refresh(c, intake)
    serialized = service.js(intake)
    c.execute('INSERT INTO packing_intakes VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (intake['id'], serialized))
    c.execute('INSERT INTO packing_intake_revisions VALUES (?,?,?)', (intake['id'], intake['version'], serialized))


def _present(c, intake):
    result = _refresh(c, copy.deepcopy(intake))
    base = '/api/packing/intakes/' + result['id']
    for source in result['sources']:
        source.pop('stored_name', None)
        source['url'] = f"{base}/sources/{source['id']}/file"
        for page in source['pages']:
            page.pop('stored_name', None)
            page['url'] = f"{base}/sources/{source['id']}/images/{page['image_id']}"
    for field in result['fields']:
        for candidate in field['candidates']:
            candidate.pop('fingerprint', None)
    result['usage'] = model_usage.summary(job_id=result['id'])
    return result


def create_intake(user, data):
    _staff(user)
    with db.transaction() as c:
        init_schema(c)
        def create():
            customer = _text(data.get('customer', ''), '客户名称或客户标识', 120, True)
            title = _text(data.get('title', '客户发货资料'), '资料名称', 200, True)
            identity = db.uid('intake')
            intake = {'id': identity, 'customer': customer, 'title': title, 'version': 1, 'status': 'needs_review',
                      'creator': user['name'], 'created_at': db.now(), 'updated_at': db.now(), 'sources': [], 'fields': [], 'lines': [],
                      'rules': {'confirmation': None}, 'history': [{'version': 1, 'operator': user['name'], 'action': '新建资料核对',
                                                               'reason': '员工选择客户并建立本地资料记录', 'at': db.now()}],
                      'report': {'title': '资料核对报告', 'model_status': 'not_started', 'messages': ['请上传原资料或粘贴聊天原文；关键字段和规则需要人工核对。']}}
            intake['fields'] = [_field(name) for name in (*TOP_FIELDS, *RULE_FIELDS)]
            if data.get('text'):
                intake['sources'].append(parsers.parse_text('粘贴聊天原文', data['text'], _role(data.get('source_role', 'shipment'))))
            _save(c, intake)
            service.audit(c, user, 'packing_intake_create', identity, None, {'customer': customer, 'version': 1})
            return _present(c, intake)
        return _operation(c, user, 'intake:create', data, create)


def list_intakes(user):
    _staff(user)
    with db.connect() as c:
        init_schema(c)
        result = []
        for row in c.execute('SELECT data FROM packing_intakes ORDER BY rowid DESC LIMIT 300'):
            intake = _refresh(c, json.loads(row[0]))
            result.append({key: intake[key] for key in ('id', 'customer', 'title', 'version', 'status', 'ready', 'created_at', 'updated_at', 'bound_task_id')})
        return result


def get_intake(user, identity):
    _staff(user)
    with db.connect() as c:
        init_schema(c)
        return _present(c, _get(c, identity))


def _add_source(c, user, identity, data, parse):
    intake = _get(c, identity)
    _check_version(intake, data)
    reason = _reason(data)
    if len(intake['sources']) >= 20:
        service.fail(422, '一组资料最多 20 份原文件或文字，请拆分后上传')
    source = parse()
    same = next((item for item in intake['sources'] if item['sha256'] == source['sha256'] and item['role'] == source['role']), None)
    if same:
        result = _present(c, intake)
        result['deduplicated'] = True
        result['report']['messages'] = ['相同原资料已存在，本次未重复添加或覆盖。', *result['report']['messages']]
        return result
    intake['sources'].append(source)
    intake['rules']['confirmation'] = None
    _touch(intake, user, '追加原资料', reason)
    intake['report'] = {'title': '资料核对报告', 'model_status': 'not_started', 'messages': ['原资料已保存，请提取并核对新增内容。', *source['notes']]}
    _save(c, intake)
    service.audit(c, user, 'packing_source_add', identity, None, {'source_id': source['id'], 'sha256': source['sha256'], 'version': intake['version']})
    return _present(c, intake)


def add_file(user, identity, filename, content, data):
    _staff(user)
    role = _role(data.get('source_role', 'shipment'))
    operation_data = {**data, 'filename': filename, 'content_sha256': hashlib.sha256(content).hexdigest()}
    with db.transaction() as c:
        init_schema(c)
        return _operation(c, user, 'intake:source:' + identity, operation_data,
                          lambda: _add_source(c, user, identity, data, lambda: parsers.parse_file(filename, content, role)))


def add_text(user, identity, data):
    _staff(user)
    role = _role(data.get('source_role', 'shipment'))
    with db.transaction() as c:
        init_schema(c)
        return _operation(c, user, 'intake:text:' + identity, data,
                          lambda: _add_source(c, user, identity, data, lambda: parsers.parse_text(data.get('name', '粘贴聊天原文'), data.get('text'), role)))


async def extract_intake(user, identity, data):
    _staff(user)
    operation = 'intake:extract:' + identity
    with db.transaction() as c:
        init_schema(c)
        key, digest, previous = _operation_key(c, user, operation, data)
        if previous is not None:
            return _present(c, _get(c, identity)) if previous.get('_pending') else previous
        intake = _get(c, identity)
        _check_version(intake, data)
        reason = _reason(data)
        if not intake['sources']:
            service.fail(422, '请先上传资料或粘贴聊天原文')
        intake['rules']['confirmation'] = None
        _touch(intake, user, '开始 AI 提取', reason)
        intake.update(status='extracting', extraction_request=key)
        _save(c, intake)
        _record_operation(c, user, operation, key, digest, {'_pending': True})
    error = None
    cancelled = None
    try:
        raw, attempt_id, images = await source_ai.extract(copy.deepcopy(intake), user, key)
        candidate = copy.deepcopy(intake)
        _apply_extraction(candidate, raw, images)
        intake = candidate
        intake['report']['attempt_id'] = attempt_id
    except asyncio.CancelledError as exc:
        cancelled = exc
        error = 'AI 提取已取消，原资料仍已保留为待核对草稿，请重新提取后核对。'
    except Exception as exc:
        from fastapi import HTTPException
        if isinstance(exc, HTTPException):
            error = str(exc.detail)
        elif isinstance(exc, model_usage.BudgetExceeded):
            error = '统一模型费用额度不足，未继续调用；原资料保留为待核对草稿'
        elif isinstance(exc, model_usage.BudgetConfigurationError):
            error = '统一模型费用配置未通过核对，未继续调用；原资料保留为待核对草稿'
        else:
            error = 'AI 提取未完成，可能是模型响应或网络问题。原资料仍已保留，请稍后重试并人工核对。'
    with db.transaction() as c:
        current = _get(c, identity)
        if current.get('extraction_request') != key or current['version'] != intake['version']:
            service.fail(409, '提取期间资料已变化，结果未应用，请重新核对')
        _touch(intake, user, 'AI 提取失败，保留草稿' if error else 'AI 提取完成，等待人工核对', reason)
        intake.pop('extraction_request', None)
        if error:
            intake['status'] = 'extraction_failed'
            intake['report'] = {'title': '资料核对报告', 'model_status': 'failed', 'messages': [error]}
        _save(c, intake)
        result = _present(c, intake)
        _record_operation(c, user, operation, key, digest, result)
    if cancelled is not None:
        raise cancelled
    return result


def confirm_fields(user, identity, data):
    _staff(user)
    with db.transaction() as c:
        init_schema(c)
        def confirm():
            intake = _get(c, identity)
            _check_version(intake, data)
            reason = _reason(data)
            changes = data.get('changes')
            if not isinstance(changes, list) or not 1 <= len(changes) <= 750:
                service.fail(422, '请逐项提交需要人工核对的字段')
            seen = set()
            for change in changes:
                if not isinstance(change, dict) or not isinstance(change.get('field_id'), str) or change.get('confirmed') is not True:
                    service.fail(422, '请明确勾选已人工核对的字段')
                field = next((item for item in intake['fields'] if item['id'] == change['field_id']), None)
                if not field or field['id'] in seen:
                    service.fail(422, '字段不存在或重复，请刷新后逐项核对')
                if field['name'] in RULE_FIELDS:
                    service.fail(422, '规则字段请通过规则与尺寸核对操作确认')
                seen.add(field['id'])
                value = _value(field['name'], change.get('value', field['value']))
                if _blank(value) and ('lines.' + field['name'] if field['line_id'] else field['name']) in CORE_REQUIRED:
                    service.fail(422, field['title'] + '为关键字段，不能确认空值')
                citations = change.get('citations')
                if citations is None:
                    same = next((candidate for candidate in field['candidates'] if candidate['value'] == value), None)
                    citations = same['citations'] if same else []
                if citations:
                    citations = _references(intake, citations)
                    for citation in citations:
                        if citation.get('image_id'):
                            citation['verification'] = '人工已对照实际页图核对'
                _append_candidate(field, value, citations, 'human', reason)
                field.update(value=value, origin='human', confirmed=True, citations=citations,
                             confirmation={'operator': user['name'], 'actor_id': user['id'], 'at': db.now(), 'reason': reason,
                                           'mode': '原资料人工核对' if citations else '人工补录或更正，见修改原因'})
                field['active_rejections'] = []
            _touch(intake, user, '人工逐项确认或更正字段', reason)
            _save(c, intake)
            service.audit(c, user, 'packing_fields_confirm', identity, None, {'field_ids': list(seen), 'version': intake['version'], 'reason': reason})
            return _present(c, intake)
        return _operation(c, user, 'intake:fields:' + identity, data, confirm)


def _confirm_mapping(c, user, data):
    if data.get('confirmed') is not True:
        service.fail(422, '请确认已核对客户、原编码、规格与内部 SKU')
    customer = _text(data.get('customer', ''), '客户名称或标识', 120, True)
    code = _value('external_code', data.get('external_code'))
    specification = _value('specification', data.get('specification'))
    if _blank(code) or _blank(specification):
        service.fail(422, '客户商品编码和原资料规格不能为空')
    sku_id = _text(data.get('sku_id', ''), '内部 SKU', 100, True)
    if not packing_sku.current(c, sku_id)['packing_enabled']:
        service.fail(422, '停用 SKU 只能人工登记本单例外，不能保存为自动复用的客户映射')
    sku = service.one(c, 'SELECT skus.id,skus.label,products.data FROM skus JOIN products ON products.id=skus.product_id WHERE skus.id=?', (sku_id,), '内部 SKU 不存在，请重新选择')
    reason = _reason(data)
    identity = _mapping_id(customer, code, specification)
    current = c.execute('SELECT version,data FROM packing_sku_mappings WHERE id=?', (identity,)).fetchone()
    if current and (type(data.get('expected_version')) is not int or data['expected_version'] != current['version']):
        service.fail(409, '该客户编码和规格的映射已存在或已变化，请核对已有映射版本后再替换')
    if not current and data.get('expected_version', 0) != 0:
        service.fail(409, '映射版本不正确，请刷新映射列表')
    before = json.loads(current['data']) if current else None
    version = current['version'] + 1 if current else 1
    product = json.loads(sku['data'])
    result = {'id': identity, 'customer': customer, 'external_code': code, 'specification': specification,
              'sku_id': sku_id, 'product_name': product['name'], 'internal_specification': sku['label'], 'version': version,
              'confirmed': True, 'operator': user['name'], 'confirmed_at': db.now(), 'reason': reason,
              'specification_difference_confirmed': specification != sku['label']}
    c.execute('INSERT INTO packing_sku_mappings VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sku_id=excluded.sku_id,version=excluded.version,data=excluded.data',
              (identity, customer, code, specification, sku_id, version, service.js(result)))
    c.execute('INSERT INTO packing_sku_mapping_revisions VALUES (?,?,?)', (identity, version, service.js(result)))
    service.audit(c, user, 'packing_sku_mapping_confirm', identity, before, result)
    return result


def list_mappings(user, customer=''):
    _staff(user)
    customer = _text(customer, '客户查询', 120)
    with db.connect() as c:
        init_schema(c)
        rows = c.execute('SELECT data FROM packing_sku_mappings WHERE customer=? ORDER BY rowid DESC LIMIT 500', (customer,)) if customer else c.execute('SELECT data FROM packing_sku_mappings ORDER BY rowid DESC LIMIT 500')
        return [json.loads(row[0]) for row in rows]


def confirm_mapping(user, data):
    _staff(user)
    with db.transaction() as c:
        init_schema(c)
        return _operation(c, user, 'mapping:confirm', data, lambda: _confirm_mapping(c, user, data))


def select_sku(user, identity, data):
    _staff(user)
    with db.transaction() as c:
        init_schema(c)
        def select():
            intake = _get(c, identity)
            _check_version(intake, data)
            reason = _reason(data)
            _refresh(c, intake)
            line = next((item for item in intake['lines'] if item['id'] == data.get('line_id')), None)
            if not line:
                service.fail(422, '请选择当前资料的商品行')
            code, spec = (_lookup(intake, name, line['id']) for name in ('external_code', 'specification'))
            if not code['confirmed'] or not spec['confirmed'] or _blank(code['value']) or _blank(spec['value']):
                service.fail(422, '请先人工确认该行客户商品编码和原资料规格')
            if data.get('specification_confirmed') is not True:
                service.fail(422, '请人工核对原资料规格与内部 SKU，包含规格差异或多个候选')
            sku_id = _text(data.get('sku_id', ''), '内部 SKU', 100, True)
            service.one(c, 'SELECT id FROM skus WHERE id=?', (sku_id,), '内部 SKU 不存在，请重新选择')
            state = packing_sku.current(c, sku_id)
            barcode = _lookup(intake, 'product_barcode', line['id'])['value'] or ''
            flags = packing_sku.issues(state, barcode)
            confirmation = data.get('sku_confirmation')
            packing_sku.review(state, barcode, confirmation)
            if any(flags.values()) and data.get('save_mapping') is True:
                service.fail(422, '停用或条码冲突的本单例外不能保存为自动复用映射，请取消保存映射')
            existing = c.execute('SELECT * FROM packing_sku_mappings WHERE customer=? AND external_code=? AND specification=?',
                                 (intake['customer'], code['value'], spec['value'])).fetchone()
            if existing and existing['sku_id'] != sku_id:
                service.fail(409, '该客户编码与规格已有另一内部 SKU 映射，请先到映射资料中说明原因并确认替换')
            if data.get('save_mapping') is True and not existing:
                _confirm_mapping(c, user, {'customer': intake['customer'], 'external_code': code['value'], 'specification': spec['value'],
                                          'sku_id': sku_id, 'reason': reason, 'confirmed': True, 'expected_version': 0})
            line['selection'] = {'customer': intake['customer'], 'external_code': code['value'], 'specification': spec['value'], 'sku_id': sku_id,
                                 'operator': user['name'], 'at': db.now(), 'reason': reason, 'specification_confirmed': True}
            if barcode or any(flags.values()):
                line['selection']['product_barcode'] = barcode
            if barcode:
                line['selection']['barcode_candidates'] = sorted(item['sku_id'] for item in _catalog(c) if item['product_barcode'] == barcode)
                line['selection']['sku_version'] = state['version']
            if any(flags.values()):
                line['selection']['sku_confirmation'] = {'version': state['version'], 'disabled_confirmed': flags['disabled'],
                                                         'barcode_conflict_confirmed': flags['barcode_conflict'], 'reason': _text(confirmation['reason'], '本单例外原因', 500, True)}
            _touch(intake, user, '人工选择并核对内部 SKU', reason)
            _save(c, intake)
            return _present(c, intake)
        return _operation(c, user, 'intake:sku:' + identity, data, select)


def confirm_rules(user, identity, data):
    _staff(user)
    with db.transaction() as c:
        init_schema(c)
        def confirm():
            intake = _get(c, identity)
            _check_version(intake, data)
            reason = _reason(data)
            _refresh(c, intake)
            if data.get('confirmed') is not True:
                service.fail(422, '请确认已对照规则资料核对必填项与标签尺寸')
            required = _value('required_fields', data.get('required_fields', intake['rules']['required_fields']))
            if not set(CORE_REQUIRED).issubset(required):
                service.fail(422, '客户编码、规格、发货数量、每箱数量与收发货资料等关键必填项不能取消')
            values = {'required_fields': required, 'label_width_mm': _value('label_width_mm', data.get('label_width_mm')),
                      'label_height_mm': _value('label_height_mm', data.get('label_height_mm'))}
            if type(values['label_width_mm']) is not int or type(values['label_height_mm']) is not int or not 100 <= values['label_width_mm'] <= 200 or not 150 <= values['label_height_mm'] <= 270:
                service.fail(422, '当前箱唛模板支持宽 100 至 200 毫米、高 150 至 270 毫米，请确认适用尺寸')
            conflicting = any(_lookup(intake, name)['conflict'] or any(candidate['value'] != values[name] for candidate in _lookup(intake, name)['candidates']) for name in RULE_FIELDS)
            if conflicting and data.get('resolve_conflicts') is not True:
                service.fail(422, '规则资料存在冲突或本次值不同，请明确确认解决规则冲突并说明原因')
            for name, value in values.items():
                field = _lookup(intake, name)
                same = next((candidate for candidate in field['candidates'] if candidate['value'] == value), None)
                citations = same['citations'] if same else []
                _append_candidate(field, value, citations, 'human', reason)
                field.update(value=value, origin='human', confirmed=True, citations=citations,
                             confirmation={'operator': user['name'], 'actor_id': user['id'], 'at': db.now(), 'reason': reason})
                field['active_rejections'] = []
            intake['rules']['confirmation'] = {'operator': user['name'], 'at': db.now(), 'reason': reason,
                                                'resolved_conflicts': data.get('resolve_conflicts') is True,
                                                'source': '规则资料人工核对' if intake['rules']['candidates'] else '人工确认的本地演示规则'}
            _touch(intake, user, '人工确认箱唛规则与标签尺寸', reason)
            _save(c, intake)
            return _present(c, intake)
        return _operation(c, user, 'intake:rules:' + identity, data, confirm)


def _ensure_ready(c, intake):
    _refresh(c, intake)
    if not intake['ready']:
        service.fail(422, '箱唛资料尚未核对完成：' + '；'.join(intake['missing'][:6] or ['存在未解决的资料冲突']))
    for source in intake['sources']:
        parsers.verify_original(source)


def _plan(c, intake):
    result = {name: _lookup(intake, name)['value'] or '' for name in TOP_FIELDS}
    result['lines'] = []
    from . import packing
    for line in intake['lines']:
        values = {name: _lookup(intake, name, line['id'])['value'] for name in LINE_FIELDS}
        row = service.one(c, 'SELECT product_id FROM skus WHERE id=?', (line['sku_id'],), '已选内部 SKU 不存在，请重新核对')
        package = packing._package(c, row['product_id'])
        planned = {'sku_id': line['sku_id'], 'external_code': values['external_code'], 'quantity': values['quantity'], 'per_carton': values['per_carton']}
        for name in PLAN_LINE_FIELDS:
            if name in ('sku_id', 'external_code', 'quantity', 'per_carton'):
                continue
            planned[name] = ((values[name] or package['requirements'].get(name, False)) if name in REQUIREMENTS else
                             values[name] or ([] if name == 'serial_numbers' else ''))
        state = packing_sku.current(c, line['sku_id'])
        planned['product_barcode'] = planned['product_barcode'] or state['product_barcode']
        confirmation = (line.get('selection') or {}).get('sku_confirmation')
        if confirmation:
            planned['sku_confirmation'] = copy.deepcopy(confirmation)
        result['lines'].append(planned)
    return result


def draft(user, identity, data):
    _staff(user)
    with db.connect() as c:
        init_schema(c)
        intake = _get(c, identity)
        _check_version(intake, data)
        _text(data.get('request_id', ''), '提交凭证', 200, True)
        _ensure_ready(c, intake)
        result = _plan(c, intake)
        result.update(intake_id=intake['id'], intake_version=intake['version'],
                      label_width_mm=intake['rules']['label_width_mm'], label_height_mm=intake['rules']['label_height_mm'],
                      bound_task_id=intake.get('bound_task_id'))
        result['layout'] = {'paper': 'a4', 'kind': 'all', 'width_mm': intake['rules']['label_width_mm'],
                            'height_mm': intake['rules']['label_height_mm'], 'show_note': True}
        return result


def _task_plan(task):
    result = {name: task.get(name, '') for name in TOP_FIELDS}
    result['lines'] = [{name: line.get(name, False if name in REQUIREMENTS else [] if name == 'serial_numbers' else '') for name in PLAN_LINE_FIELDS}
                       for line in task.get('lines', [])]
    return result


def _snapshot(c, intake):
    sources = []
    for source in intake['sources']:
        item = {key: copy.deepcopy(source[key]) for key in ('id', 'name', 'kind', 'role', 'sha256', 'chunks', 'pages')}
        item['url'] = f"/api/packing/intakes/{intake['id']}/sources/{source['id']}/file"
        for page in item['pages']:
            page.pop('stored_name', None)
            page['url'] = f"/api/packing/intakes/{intake['id']}/sources/{source['id']}/images/{page['image_id']}"
        sources.append(item)
    return {'intake_id': intake['id'], 'intake_version': intake['version'], 'customer': intake['customer'], 'title': intake['title'],
            'sources': sources,
            'fields': copy.deepcopy(intake['fields']), 'lines': copy.deepcopy(intake['lines']), 'rules': copy.deepcopy(intake['rules']),
            'plan': _plan(c, intake), 'confirmed_at': intake['updated_at']}


def _digest(c, intake):
    return _hash(_snapshot(c, intake))


def bind_task(c, user, task, input_data):
    """Called after normalization/revision assignment, before persisting task."""
    _staff(user)
    init_schema(c)
    identity = input_data.get('intake_id')
    prior = c.execute('SELECT 1 FROM packing_source_bindings WHERE task_id=? LIMIT 1', (task.get('id'),)).fetchone()
    if not identity:
        if prior or task.get('intake_snapshot'):
            service.fail(409, '此任务已绑定原资料，修改时请携带资料编号并先完成资料更正与人工核对')
        task.pop('intake_snapshot', None)
        return task
    intake = _get(c, identity)
    already = c.execute('SELECT task_id FROM packing_source_bindings WHERE intake_id=? AND task_id<>? LIMIT 1', (intake['id'], task.get('id'))).fetchone()
    if already:
        service.fail(409, '该资料已建立装箱任务，请打开原任务并提交变更，不能重复建立发货订单')
    if type(input_data.get('intake_version')) is not int or input_data['intake_version'] != intake['version']:
        service.fail(409, '绑定的资料版本已变化，请重新应用已核对资料')
    _ensure_ready(c, intake)
    expected = _task_plan(_plan(c, intake))
    if _task_plan(task) != expected:
        service.fail(422, '装箱任务的实际商品行、数量或收发货字段与已确认原资料不一致，请先更正资料并重新人工核对')
    if not isinstance(task.get('id'), str) or type(task.get('revision')) is not int or task['revision'] < 1:
        service.fail(422, '装箱任务版本格式不正确，无法绑定原资料')
    snapshot = _snapshot(c, intake)
    snapshot['task_revision'] = task['revision']
    snapshot['bound_by'] = user['name']
    snapshot['bound_at'] = db.now()
    digest = _digest(c, intake)
    existing = c.execute('SELECT data FROM packing_source_bindings WHERE task_id=? AND revision=?', (task['id'], task['revision'])).fetchone()
    if existing:
        if json.loads(existing['data'])['intake_snapshot'] != task.get('intake_snapshot'):
            service.fail(409, '当前任务版本已有不可替换的原资料绑定，请建立新修订版本')
        return task
    task['intake_snapshot'] = snapshot
    c.execute('INSERT INTO packing_source_bindings VALUES (?,?,?,?,?,?)',
              (task['id'], task['revision'], intake['id'], intake['version'], digest, service.js({'intake_snapshot': snapshot, 'plan': expected})))
    return task


def validate_for_approval(c, task):
    """No client-provided snapshot or order-of-upload can bypass this gate."""
    init_schema(c)
    row = c.execute('SELECT * FROM packing_source_bindings WHERE task_id=? AND revision=?', (task.get('id'), task.get('revision'))).fetchone()
    prior = c.execute('SELECT 1 FROM packing_source_bindings WHERE task_id=? LIMIT 1', (task.get('id'),)).fetchone()
    snapshot = task.get('intake_snapshot')
    if not row:
        if snapshot or prior:
            service.fail(409, '任务的原资料绑定缺失或修订已变化，请重新核对并绑定当前版本')
        return
    saved = json.loads(row['data'])
    if not isinstance(snapshot, dict) or snapshot != saved['intake_snapshot']:
        service.fail(409, '任务来源快照不符，不能采用客户端提供的来源记录放行')
    intake = _get(c, row['intake_id'])
    if intake['version'] != row['intake_version']:
        service.fail(409, '关联原资料已更新，当前审核和标签不能继续放行，请重新核对当前资料版本')
    _ensure_ready(c, intake)
    if _digest(c, intake) != row['digest'] or _task_plan(task) != saved['plan']:
        service.fail(422, '任务实际值、SKU 映射或规则与已确认原资料不一致，不能审核或生成有效箱唛')
    layout = task.get('layout', {})
    if layout and (layout.get('width_mm', 100) != snapshot['rules']['label_width_mm'] or layout.get('height_mm', 150) != snapshot['rules']['label_height_mm']):
        service.fail(422, '任务打印尺寸与已确认规则资料不一致，请更正版式后重新核对')


def validate_document_rules(c, task, data):
    validate_for_approval(c, task)
    if not task.get('intake_snapshot'):
        return
    rules = task['intake_snapshot']['rules']
    if data.get('width_mm', 100) != rules['label_width_mm'] or data.get('height_mm', 150) != rules['label_height_mm']:
        service.fail(422, '标签尺寸与已确认规则资料不一致，请采用已确认的标签宽高')


def source_file(user, identity, source_id):
    _staff(user)
    with db.connect() as c:
        intake = _get(c, identity)
        source = next((item for item in intake['sources'] if item['id'] == source_id), None)
        if not source:
            service.fail(404, '原资料文件不存在')
        return parsers.verify_original(source), source['mime_type'], source['name']


def source_image(user, identity, source_id, image_id):
    _staff(user)
    with db.connect() as c:
        intake = _get(c, identity)
        source = next((item for item in intake['sources'] if item['id'] == source_id), None)
        page = next((item for item in source['pages'] if item['image_id'] == image_id), None) if source else None
        if not page:
            service.fail(404, '该资料原图或扫描页不存在')
        path = parsers.storage_path(page['stored_name'])
        if not path.is_file():
            service.fail(404, '原图或扫描页文件缺失')
        return path, page['mime_type']


def build_router(user_dependency):
    router = APIRouter(prefix='/api/packing', tags=['箱唛资料核对'])

    @router.get('/intakes')
    def items(u=Depends(user_dependency)):
        return list_intakes(u)

    @router.post('/intakes', status_code=201)
    def create(data: dict = Body(...), u=Depends(user_dependency)):
        return create_intake(u, data)

    @router.get('/intakes/{identity}')
    def item(identity: str, u=Depends(user_dependency)):
        return get_intake(u, identity)

    @router.post('/intakes/{identity}/sources')
    async def upload(identity: str, file: UploadFile = File(...), version: int = Form(...), reason: str = Form(...),
                     request_id: str = Form(...), source_role: str = Form('shipment'), u=Depends(user_dependency)):
        _staff(u)
        try:
            content = await file.read(parsers.MAX_FILE_BYTES + 1)
            return add_file(u, identity, file.filename, content,
                            {'version': version, 'reason': reason, 'request_id': request_id, 'source_role': source_role})
        finally:
            await file.close()

    @router.post('/intakes/{identity}/texts')
    def text(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return add_text(u, identity, data)

    @router.post('/intakes/{identity}/extract')
    async def extract(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return await extract_intake(u, identity, data)

    @router.post('/intakes/{identity}/fields/confirm')
    def fields(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return confirm_fields(u, identity, data)

    @router.post('/intakes/{identity}/sku')
    def sku(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return select_sku(u, identity, data)

    @router.post('/intakes/{identity}/rules/confirm')
    def rules(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return confirm_rules(u, identity, data)

    @router.post('/intakes/{identity}/draft')
    def plan(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return draft(u, identity, data)

    @router.get('/intakes/{identity}/sources/{source_id}/file')
    def original(identity: str, source_id: str, u=Depends(user_dependency)):
        path, mime, name = source_file(u, identity, source_id)
        return FileResponse(path, media_type=mime, filename=name, headers={'X-Content-Type-Options': 'nosniff'})

    @router.get('/intakes/{identity}/sources/{source_id}/images/{image_id}')
    def image(identity: str, source_id: str, image_id: str, u=Depends(user_dependency)):
        path, mime = source_image(u, identity, source_id, image_id)
        return FileResponse(path, media_type=mime, headers={'X-Content-Type-Options': 'nosniff'})

    @router.get('/mappings')
    def mappings(customer: str = '', u=Depends(user_dependency)):
        return list_mappings(u, customer)

    @router.post('/mappings/confirm')
    def mapping(data: dict = Body(...), u=Depends(user_dependency)):
        return confirm_mapping(u, data)

    return router
