"""Project a requested, verified fallback without changing stored preferences.

A saved "if unavailable" request is not a global preference. Only its current
customer request and a fresh complete catalogue may activate its local rules.
"""
from copy import deepcopy
import re


def _key(value):
    return re.sub(r'\s+', '', str(value)).casefold().strip('，,。！？?')


def _unavailable(subject, products):
    needle = _key(subject)
    if not needle:
        return False, set()
    exact = [p for p in products if any(_key(alias) == needle for alias in
             [p.get('id', ''), p.get('model', ''), p.get('name', ''), *p.get('aliases', [])])]
    if exact:
        skus = [s for p in exact for s in p.get('skus', [])]
        # Missing inventory is not evidence of being unavailable.
        return (bool(skus) and all(type(s.get('stock')) is int and s['stock'] == 0 for s in skus),
                {p['id'] for p in exact})
    # A full unlisted model is distinct from a shorter known model. Broad or
    # partial names, however, must not turn an existing product into "absent".
    if not re.fullmatch(r'[a-z]{1,8}\d+[a-z0-9._-]*', needle):
        if any(needle in _key(alias) or _key(alias) in needle for p in products
               for alias in [p.get('name', ''), p.get('model', ''), *p.get('aliases', [])] if alias):
            return False, set()
    return True, set()


def active_requests(ctx):
    if ctx.get('catalog_evidence', {}).get('source') != 'fresh_database':
        return []
    products = ctx.get('products', [])
    if not products:
        return []
    latest = next((m for m in reversed(ctx.get('messages', [])) if m.get('role') == 'user'), None)
    if not latest or not latest.get('id'):
        return []
    result = []
    for record in ctx.get('preferences', {}).get('conditional_requests', []):
        if record.get('active') is not True:
            continue
        # Unrelated follow-ups do not activate old fallback budgets. An edited
        # request retains its original text; its current values remain binding.
        if (record.get('source_message_id') != latest['id']
                and _key(latest.get('content', '')) not in {
                    _key(record.get('source_text', '')), _key(record.get('original_source_text', ''))}):
            continue
        condition = record.get('condition') or {}
        if condition.get('kind') != 'catalogue_unavailable':
            continue
        condition_met, original_ids = _unavailable(condition.get('subject_text', ''), products)
        if condition_met:
            result.append((record, original_ids))
    return result


def items(ctx):
    result = []
    products = ctx.get('products', [])
    for record, original_ids in active_requests(ctx):
        for product in products:
            if product['id'] in original_ids or record.get('scope') not in ('会话', product.get('category')):
                continue
            for key, value in record.get('constraints', {}).items():
                if value is None or value == []:
                    continue
                source = record.get('constraint_sources', {}).get(key) or record
                result.append({'key': key, 'value': deepcopy(value), 'scope': record['scope'],
                    'product_id': product['id'], 'active': True, 'source_message_id': source.get('source_message_id'),
                    'source_text': source.get('source_text'), 'basis': 'verified_conditional_alternative',
                    'conditional_request_id': record.get('id')})
    return result
