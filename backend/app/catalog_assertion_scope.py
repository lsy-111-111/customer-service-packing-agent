"""Read-only evidence for shared catalogue assertions.

This module never quotes, chooses products or mutates saved observations. An
unknown package field stays unknown; current unit prices and payable quotes
remain different kinds of evidence.
"""
from copy import deepcopy
from decimal import Decimal
import re


def shared_reference(sentence):
    """Return (present, cardinality) for an actual shared product reference."""
    count = re.search(r'([两二三四五六七八九十\d]+)款', sentence)
    if count:
        numbers = dict(zip('两二三四五六七八九十', (2, 2, 3, 4, 5, 6, 7, 8, 9, 10)))
        return True, int(count[1]) if count[1].isdigit() else numbers.get(count[1])
    if re.search(r'两者|二者', sentence):
        return True, 2
    if re.search(r'它们|这几款|这些(?:型号|商品)|上述(?:型号|商品)|以上(?:型号|商品)', sentence):
        return True, None
    return False, None


def lid_state(product):
    specs = product.get('specs') or {}
    specs = specs if isinstance(specs, dict) else {}
    package = product.get('package_contents') or []
    package = package if isinstance(package, (list, tuple)) else [package]
    records = [str(value) for value in specs.values()] + [str(value) for value in package]
    records.extend(str(key) + '：' + str(value) for key, value in specs.items() if '杯盖' in str(key))
    positive, negative = False, False
    for record in records:
        if '杯盖' not in record or re.search(r'未(?:明确)?(?:记录|记载|注明)|未知|待核实|无法确认|是否', record):
            continue
        absent = bool(re.search(r'(?:不含|不包含|不附带|不带|不带有|不配备|不配有|无|没有)杯盖|杯盖\s*[：:]?\s*(?:无|没有|不含)', record))
        negative |= absent
        positive |= (not absent and bool(re.search(r'(?:含|附带|配有|配备|自带|包含|带有)杯盖|杯盖\s*[：:]\s*(?:有|含|附带|配有|\d+)', record)))
    # A named item in the packaging list is affirmative inclusion evidence.
    positive |= any('杯盖' in str(item) and not re.search(r'不含|不包含|不带|不附带|不配备|不配有|无杯盖|没有|未|未知|是否|待核|选配|另购|可选', str(item))
                    for item in package)
    if positive == negative:
        return 'unknown'
    return 'included' if positive else 'absent'


def capacity_ml(product):
    specs = product.get('specs') or {}
    if not isinstance(specs, dict):
        return None
    values = [str(value) for key, value in specs.items() if re.search(r'容量|capacity|volume', str(key), re.I)]
    amounts = set()
    for value in values:
        if re.search(r'未|未知|待核|约|可选|范围|\d\s*(?:/|~|～|至|–|—|-)\s*\d', value):
            continue
        matches = re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)\s*(m[lL]|毫升|[lL]|升)(?![A-Za-z])', value, re.I)
        amounts.update(Decimal(number) * (1000 if unit in ('l', 'L', '升') else 1)
                       for number, unit in matches)
    return next(iter(amounts)) if len(amounts) == 1 else None


def ground_lid_statement(sentence, members):
    if len(members) != 2 or '杯盖' not in sentence:
        return sentence
    states = [lid_state(product) for product in members]
    notes = []
    for product, state in zip(members, states):
        name = str(product.get('model') or product['id'])
        notes.append(name + ('明确不含杯盖' if state == 'absent' else '明确附带杯盖' if state == 'included'
                             else '的资料未明确记载杯盖，不能判断是否附带'))
    note = '；'.join(notes)
    # Only rewrite the assertion about the pair. Independent stock, price,
    # care and service clauses retain their original text and their own owner.
    contrast = re.compile(r'(?:主要区别|主要差别|差别主要|区别主要|区别|差别)(?:是|在|为)?'
                          r'(?P<capacity>容量(?:和|与|、))?(?:是否)?(?:含|带|附带)?杯盖')
    capacities = [capacity_ml(product) for product in members]
    def repair(match):
        capacity_note = ''
        if match['capacity']:
            capacity_note = ('已记录的容量相同；' if capacities[0] is not None and capacities[0] == capacities[1]
                             else '已记录的容量不同；' if all(value is not None for value in capacities)
                             else '容量差异尚无完整记录；')
        if 'unknown' not in states:
            return capacity_note + ('杯盖配置记录不同：' if states[0] != states[1]
                                    else '杯盖配置记录相同：') + note
        return capacity_note + note
    sentence = contrast.sub(repair, sentence)
    shared = (r'(?:两款|这两款|两者|二者|它们)(?:都|均)'
              r'(?P<predicate>不含|不包含|不附带|不带|不带有|不配备|不配有|无|包含|带有|配备|附带|含|配有)杯盖')
    def repair_shared(match):
        claimed = 'absent' if match['predicate'].startswith(('不', '无')) else 'included'
        return match.group() if all(state == claimed for state in states) else note
    sentence = re.sub(shared, repair_shared, sentence)
    if 'unknown' in states:
        sentence = re.sub(r'按容量和是否需要杯盖来选就好', '可按已记录容量选择；' + note, sentence)
    # A contrast followed immediately by a repeated shared claim can produce
    # the same evidence note twice. Collapse only the identical generated
    # adjacent note, preserving intervening price, stock or service clauses.
    sentence = re.sub(re.escape(note) + r'\s*[：:，,；;]\s*' + re.escape(note),
                      lambda match: note, sentence)
    return sentence


def price_observations(ctx, facts, normalize):
    """Project ordered unit fields and latest exact SKU/count quotes.

    A later malformed quote invalidates that exact observation. A complete
    product replacement with a missing/changed price cannot revive an old
    quote. Different quantities never overwrite one another merely because
    they refer to the same product.
    """
    products = {p['id']: deepcopy(p) for p in ctx.get('products', [])}
    units = ({s['id']: s.get('price_cents') for p in products.values() for s in p.get('skus', [])}
             if ctx.get('catalog_evidence', {}).get('source') == 'fresh_database' else {})
    quotes = {}
    for fact in facts or []:
        value, tool = fact.get('result'), fact.get('tool')
        if not isinstance(value, dict):
            continue
        if tool == 'get_product' and value.get('id') in products and not value.get('error'):
            old = products[value['id']]
            old_ids = {s['id'] for s in old.get('skus', [])}
            products[value['id']] = deepcopy(value)
            current = {s['id']: s for s in value.get('skus', [])}
            for sid in old_ids:
                units.pop(sid, None)
            units.update((sid, sku.get('price_cents')) for sid, sku in current.items())
            for key, quote in list(quotes.items()):
                if key[0] not in old_ids:
                    continue
                sku = current.get(key[0])
                if (not sku or type(sku.get('price_cents')) is not int
                        or sku['price_cents'] != quote['unit_price_cents']
                        or type(sku.get('version')) is int and type(quote.get('sku_version')) is int
                        and sku['version'] != quote['sku_version']):
                    quotes.pop(key)
        elif tool == 'quote_price' and value.get('sku_id'):
            sid, quantity = value['sku_id'], value.get('quantity')
            normalized = normalize(value)
            if type(quantity) is int:
                quotes.pop((sid, quantity), None)
            else:
                quotes = {key: q for key, q in quotes.items() if key[0] != sid}
            if normalized:
                for key, quote in list(quotes.items()):
                    if key[0] == sid and (quote['unit_price_cents'] != normalized['unit_price_cents']
                            or type(quote.get('sku_version')) is int and type(normalized.get('sku_version')) is int
                            and quote['sku_version'] != normalized['sku_version']):
                        quotes.pop(key)
                units[sid] = normalized['unit_price_cents']
                quotes[(sid, normalized['quantity'])] = normalized
            elif 'unit_price_cents' in value:
                units[sid] = None
    return {**ctx, 'products': list(products.values())}, list(quotes.values()), units
