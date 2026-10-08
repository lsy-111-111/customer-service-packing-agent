"""Customer-sourced constraints on shopping cards, independent of model prose.

The caller supplies the current catalogue and persisted, owner-checked quotes.
An absent specification is unknown, never evidence of a missing capability.
Cards remain visible for comparison; only verified relevant choices get a CTA.
"""
import copy
import re

from . import conversation_state, shopping_state, conditional_constraints


def feature_evidence(product, feature):
    """Return true/false/unknown with the exact catalogue field as evidence."""
    needle = re.escape(feature)
    if feature == '入耳式': needle = r'(?<!半)入耳(?:式)?'
    elif feature == '半入耳': needle = r'半入耳(?:式)?'
    positives, negatives = [], []
    for field, raw in product.get('specs', {}).items():
        value = str(raw)
        if re.fullmatch(needle, str(field), re.I) and not re.search(needle, value, re.I):
            # A field name alone is not a positive capability assertion.
            explicit = value.strip().lower()
            evidence = {'field':str(field), 'value':value}
            if raw is True or explicit in ('true','yes','是','有','支持','具备','包含','提供'):
                positives.append(evidence)
            elif raw is False or explicit in ('false','no','否','无','不支持','不包含','不含','不具备','没有','非'):
                negatives.append(evidence)
            continue
        for clause in re.split(r'[，,。；;/]', value):
            matches = list(re.finditer(needle, clause, re.I))
            if not matches and re.fullmatch(needle, str(field), re.I):
                clause = str(field) + '：' + clause
                matches = list(re.finditer(needle, clause, re.I))
            for match in matches:
                before, after = clause[:match.start()], clause[match.end():]
                if re.search(r'未知|待确认|待核实|未明确|未记载|未记录|不确定|尚不清楚|是否|能否|并非不|不是不', clause):
                    continue
                negative = (re.search(r'(?:无|不支持|没有|不具备|未支持|不包含|不含|不带|非)\s*$', before)
                            or re.match(r'\s*[：:]?\s*(?:不支持|无|没有|不具备|未支持|不包含|不含|否|false\b|no\b)', after, re.I))
                (negatives if negative else positives).append({'field': str(field), 'value': value})
    if positives and negatives: return None, positives + negatives
    if positives: return True, positives
    if negatives: return False, negatives
    # Wearing styles are explicitly mutually exclusive; no such inference is
    # made for capabilities (ANC does not establish silicone tips, for example).
    wearing = str(product.get('specs', {}).get('佩戴', ''))
    opposite = '半入耳' if feature == '入耳式' else '入耳式' if feature == '半入耳' else None
    if opposite and re.fullmatch(re.escape(opposite) + r'(?:式)?', wearing):
        return False, [{'field': '佩戴', 'value': wearing}]
    return None, []


def _source_items(ctx):
    users = {m.get('id'): m for m in ctx.get('messages', []) if m.get('role') == 'user'}
    result = []
    # Use only the authoritative current state. Tombstones must not resurrect
    # older preferences, even when older customer messages still contain them.
    for item in [*ctx.get('preferences', {}).get('items', []), *conversation_state.choice_items(ctx.get('preferences', {})),
                 *conditional_constraints.items(ctx)]:
        source = item.get('source_message_id', '')
        if item.get('active') is not True or item.get('value') is None: continue
        if source not in users and source not in ctx.get('customer_edit_sources', []): continue
        result.append(item)
    return result


def _relevant_products(ctx, items):
    products = ctx.get('products', [])
    messages = [m for m in ctx.get('messages', []) if m.get('role') == 'user']
    text = messages[-1].get('content', '') if messages else ''
    named = set(conversation_state._products(text, products))
    categories = set()
    for pattern, category in [(r'耳机|耳塞|数码|鼠标|键盘', '数码'), (r'水壶|风扇|家电', '家电'),
                              (r'衣服|服饰|衬衫|短袖|长袖|外套|T恤', '服饰'), (r'杯子|水杯|毛巾|家居', '家居')]:
        if re.search(pattern, text, re.I): categories.add(category)
    if re.search(r'回到|沿用|最早|最初|原先|之前', text) and re.search(r'条件|要求|偏好|必需功能|需要的功能|需求|场景', text):
        categories.update(i['scope'] for i in items if i.get('scope') != '会话')
        goal = ctx.get('preferences', {}).get('shopping', {}).get('goal') or {}
        if goal.get('source_message_id') in {m.get('id') for m in messages}:
            named.update(conversation_state._products(goal.get('text', ''), products))
    # Explicit references may inherit customer selections. A broad question
    # such as "当前缺货能保证何时补货" must not inherit the last unrelated mug.
    if not named and not categories and (re.search(r'这款|那款|这两|那两|刚才|它|更改后的选择|最后(?:确认|选定|选择)', text)
            or re.fullmatch(r'\s*(?:那|请问|请|再查|现在)?(?:多少钱|有货吗|还有货吗|什么颜色|价格呢|库存呢)[？?。！!\s]*', text)):
        selections = ctx.get('preferences', {}).get('shopping', {}).get('selections', [])
        named.update(s['product_id'] for s in selections if s.get('source_message_id') in {m.get('id') for m in messages})
        if not named:
            for message in reversed(messages[:-1]):
                named.update(conversation_state._products(message.get('content', ''), products))
                if named: break
        if not named and ctx.get('selected_product_id'): named.add(ctx['selected_product_id'])
    # Keep every explicitly requested item in mixed-intent questions, including
    # a product associated with an order the customer names this turn.
    named.update(o['product_id'] for o in ctx.get('orders', []) if o.get('id') and o['id'] in text)
    return named | {p['id'] for p in products if p.get('category') in categories}, bool(named or categories)


def _color(value):
    return shopping_state.color_key(value)


def _reasons(product, sku, items, quotes):
    mismatches, unknown = [], []
    def add(target, item, text, evidence=None):
        target.append({'key': item['key'], 'scope': item['scope'], 'source_message_id': item['source_message_id'],
                       'product_id': item.get('product_id'), 'source_text': item.get('source_text'), 'basis':item.get('basis', 'preference'),
                       'reason': text, 'catalogue_evidence': evidence or []})
    for item in conversation_state.effective_items({'items': items}, product):
        key, value = item.get('key'), item.get('value')
        if key == 'excluded_products' and product['id'] in value:
            add(mismatches, item, '顾客已明确排除此型号')
        elif key in ('colors', 'excluded_colors') and isinstance(value, list):
            actual = sku.get('color')
            if not actual:
                add(unknown, item, '此规格颜色尚未记录，需核实是否符合颜色条件')
            elif (key == 'colors' and _color(actual) not in {_color(v) for v in value}
                  or key == 'excluded_colors' and _color(actual) in {_color(v) for v in value}):
                add(mismatches, item, '此规格颜色“' + actual + '”不符合当前颜色条件', [{'field':'sku.color', 'value':actual}])
        elif key in ('sizes', 'excluded_sizes') and isinstance(value, list):
            actual = sku.get('size')
            if not actual:
                add(unknown, item, '此规格尺码尚未记录，需核实是否符合个人尺码条件')
            elif (key == 'sizes' and str(actual).upper() not in {v.upper() for v in value}
                  or key == 'excluded_sizes' and str(actual).upper() in {v.upper() for v in value}):
                add(mismatches, item, '此规格尺码“' + str(actual) + '”不符合当前个人尺码条件', [{'field':'sku.size', 'value':actual}])
        elif key in ('required_features', 'excluded_features') and isinstance(value, list):
            for feature in value:
                supported, evidence = feature_evidence(product, feature)
                if supported is None:
                    add(unknown, item, '“' + feature + '”资料未明确，需核实是否符合条件', evidence)
                elif supported == (key == 'excluded_features'):
                    add(mismatches, item, ('已记录不支持所需功能“' if key == 'required_features' else '已记录包含顾客排除的功能“') + feature + '”', evidence)
        elif key == 'budget_max_cents' and type(value) is int:
            known = quotes.get(sku['id'])
            if known is None:
                add(unknown, item, '尚无此规格的已核实实付报价，无法确认符合预算')
            elif known['total_cents'] > value:
                add(mismatches, item, f'此报价{known["quantity"]}件实付{known["total_cents"]/100:g}元，超过当前预算{value/100:g}元',
                    [{'field':'quote.total_cents', 'value':known['total_cents'], 'quote_id':known['quote_id'], 'quantity':known['quantity']}])
    return mismatches, unknown


def _broader_than_requested_quote(ctx, product, allowed):
    """A quote's variant is query context, never a persisted personal choice."""
    sources = {m.get('id') for m in ctx.get('messages', []) if m.get('role') == 'user'}
    sources.update(ctx.get('customer_edit_sources', []))
    requests = [s for s in ctx.get('preferences', {}).get('shopping', {}).get('selections', [])
                if s.get('selection_kind') == 'quote_request' and s.get('product_id') == product['id']
                and s.get('source_message_id') in sources and (s.get('colors') or s.get('sizes'))]
    def matches(sku, request):
        return ((not request.get('colors') or _color(sku.get('color')) in {_color(c) for c in request['colors']})
                and (not request.get('sizes') or str(sku.get('size', '')).upper() in {s.upper() for s in request['sizes']}))
    return bool(requests and any(sku['id'] in allowed and not any(matches(sku, request) for request in requests)
                                 for sku in product.get('skus', [])))


def annotate(cards, ctx, verified_quotes):
    """Return new cards with server-authored metadata; never discard a card."""
    result = copy.deepcopy(cards)
    items = _source_items(ctx)
    relevant, has_explicit_scope = _relevant_products(ctx, items)
    products = {p['id']: p for p in ctx.get('products', [])}
    skus = {s['id']:(p, s) for p in products.values() for s in p.get('skus', [])}
    # Product cards use only one-unit offers; a bulk quote cannot accidentally
    # exclude a product when the customer's per-item budget would be satisfied.
    unit_quotes = {q['sku_id']:q for q in verified_quotes.values() if q.get('quantity') == 1}
    for card in result:
        if card.get('type') not in ('product', 'quote'): continue
        data = card.get('data') or {}
        quote = verified_quotes.get(data.get('quote_id')) if card['type'] == 'quote' else None
        product, sku = skus.get(quote.get('sku_id') if quote else data.get('sku_id'), (None, None)) if card['type']=='quote' else (products.get(data.get('id')), None)
        mismatches, unknown, allowed = [], [], []
        if not product or (card['type']=='quote' and not quote):
            unknown.append({'reason':'商品或报价尚未核实，暂不能确认购买'})
        else:
            # Amounts retain their audited quote; availability uses the fresh
            # SKU snapshot read for this publication, not the quote's old stock.
            if quote:
                card['data'] = {**data, **quote, 'stock': sku.get('stock') if sku else None,
                                'restock_at': sku.get('restock_at') if sku else None}
            elif product:
                card['data'] = copy.deepcopy(product)
                if any(s.get('id') == data.get('selected_sku_id') for s in product.get('skus', [])):
                    card['data']['selected_sku_id'] = data['selected_sku_id']
            options = [sku] if sku else product.get('skus', [])
            option_results = []
            for option in options:
                bad, pending = _reasons(product, option, items, {sku['id']:quote} if quote else unit_quotes)
                if option.get('stock') == 0:
                    pending.append({'reason':'此规格暂时缺货，不能直接确认购买', 'catalogue_evidence':[{'field':'sku.stock', 'value':0}]})
                elif type(option.get('stock')) is not int or option['stock'] < 0:
                    pending.append({'reason':'此规格库存尚未核实，不能直接确认购买', 'catalogue_evidence':[{'field':'sku.stock', 'value':option.get('stock')}]})
                elif quote and option['stock'] < quote.get('quantity', 1):
                    pending.append({'reason':'当前库存不足所报数量，不能直接确认购买', 'catalogue_evidence':[{'field':'sku.stock', 'value':option['stock']}]})
                option_results.append((option, bad, pending))
                if not bad and not pending: allowed.append(option['id'])
            if not allowed:
                mismatches = [r for _,bad,_ in option_results for r in bad]
                unknown = [r for _,_,pending in option_results for r in pending]
            elif not quote and len(allowed) < len(options):
                unknown = [{'reason':'部分规格不符合条件或尚待核实，仅可选择已核实规格'}]
        unrelated = bool(product and has_explicit_scope and product['id'] not in relevant)
        status = 'comparison' if mismatches else 'reference' if unrelated else 'unverified' if unknown and not allowed else 'eligible'
        label = {'comparison':'不符合当前条件，仅供对比', 'reference':'本轮未请求，仅供参考',
                 'unverified':'条件尚待核实，仅供参考', 'eligible':''}[status]
        if status == 'eligible' and unknown: label = '请从符合当前条件的规格中选择'
        if status == 'eligible' and not has_explicit_scope: label = '查询参考资料，尚未确认本轮选择'
        if quote and sku and sku.get('stock') == 0 and not mismatches: label = '此规格暂时缺货，仅供参考'
        quote_reference = bool(card['type'] == 'product' and product and status == 'eligible'
                               and _broader_than_requested_quote(ctx, product, allowed))
        if quote_reference:
            status, label = 'reference', '商品全部规格，另选需重新查询'
        reasons = list(dict.fromkeys(r['reason'] for r in [*mismatches, *unknown]))
        if quote_reference: reasons.append('本次规格报价未核实该商品的全部可选规格；其他规格需重新查询价格、库存和适用条件')
        if unrelated: reasons.append('此商品未在本轮明确请求范围内，不作为本轮推荐')
        if not quote and any(i.get('key') == 'budget_max_cents' and i.get('scope') in ('会话', product.get('category') if product else None) for i in conversation_state.effective_items({'items':items}, product or {})) and allowed:
            reasons.append('预算仅按已核实的1件报价核对，实际购买数量变化需重新核价')
            if not label: label = '预算按已核实报价核对'
        card['selection'] = {'status':status, 'label':label, 'reasons':reasons,
                             'quote_verified':bool(quote) if card['type']=='quote' else None,
                             'purchase_allowed':bool(allowed) and status == 'eligible',
                             'eligible_sku_ids':allowed if status == 'eligible' else [],
                             'constraints':mismatches + unknown,
                             'preference_version':ctx.get('preferences', {}).get('version')}
        if quote:
            stock = sku.get('stock') if sku else None
            card['data']['available'] = type(stock) is int and stock >= quote.get('quantity', 1)
            card['data']['purchase_allowed'] = card['selection']['purchase_allowed']
        elif card['data'].get('selected_sku_id') not in card['selection']['eligible_sku_ids']:
            card['data'].pop('selected_sku_id', None)
    return result
