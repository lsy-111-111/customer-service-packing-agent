"""Pure product/SKU ownership of customer-visible recommendation information.

Deleting a subject deletes its dependent information, not just a noun or a
fixed list of keywords. A surviving block with unverified orphan information
can be rendered only from its explicit, eligible SKU and current evidence.
Other intents and requested comparisons remain outside that repair.
"""
from dataclasses import dataclass, field
import re

from . import amounts, card_constraints, conversation_state, shopping_state


_FOOTER = re.compile(r'^\s*(?:以上|上述|这些(?:价格|报价|库存)|最终以|以实际|有预算上限|如有预算|本系统|本店没有|本店不支持)')
_CONCLUSION = re.compile(r'^\s*(?:所以|因此|故|按(?:你|您)(?:的)?(?:要求|条件))[^。\n]{0,18}(?:排除|不考虑|不推荐|不作为)')
_PRICE_UNKNOWN = re.compile(r'(?:单价|小计|优惠|实付|报价|价格)[^，,。；;\n]{0,12}(?:尚未|未|待)核实|不能确认是否满足[^。\n]{0,16}预算')
_REQUOTE = re.compile(r'(?:仍|还)?(?:需要|需|须|必须)(?:按(?:具体)?(?:规格|数量|型号))?(?:再|重新)(?:核价|报价)')
_FUTURE_QUOTE = re.compile(r'若|如果|假如|一旦|明天|后天|下周|下月|下次|未来|之后|以后|届时|到时'
                          r'|(?:补货|到货)(?:后|时)'
                          r'|(?:下单|购买)时|(?:数量|规格|颜色|尺寸).{0,8}(?:改变|变更|调整|改成)'
                          r'|(?:变更|调整|改换|换成|改成|改变).{0,8}(?:数量|规格|颜色|尺寸)'
                          r'|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}')
_RESTOCK = re.compile(r'补货|到货日期')
_NUMERIC_FIELD = re.compile(r'(\d+(?:\.\d+)?)\s*(小时|分钟|毫升|m[lL]|[wW]|档|°[cC])')
_STOCK_PREDICATE = r'(?:库存|现货|可售|(?:还|仅|只)?剩(?:余)?)\s*(?:为|有)?\s*'
_STOCK = re.compile(_STOCK_PREDICATE + r'(\d+)\s*(?:件|台|副|个|套|只)?')
_STOCK_QUANTITY = re.compile(_STOCK_PREDICATE + r'(?:\d+|[零〇一二两三四五六七八九十百千万]+)\s*(?:件|台|副|个|套|只)')
_CONDITIONAL = re.compile(r'^\s*(?:[-*]\s*)?(?:如果|假如|若|倘若|要是|[^，,。；;！？!?\n]{1,24}的话)')
_CONSEQUENT = re.compile(r'那么|则|就(?:可|应|需|能|要)|建议|推荐|可以|可(?:选|买|考虑|核实)|请|须|必须'
                         r'|重新(?:核价|报价)|不(?:会|能|符合)|不能|无法|仍(?:可|需|应)|优先|由人工'
                         r'|[，,；;：:\n]\s*(?:需要|应当|应该|得|要)\S')
_REVIEW_RULE = re.compile(r'^\s*(?:同时|另外|此外|以及|顺便)?\s*(?:申请|该申请|这些申请|处理事项)'
                          r'.{0,12}(?:须|需|必须).{0,5}人工(?:审核|复核|终审|确认)')
_EXPLAIN_EXCLUDED = re.compile(r'(?:不合适|不适合|不符合|不满足).{0,12}(?:直说|说清|明确|说明|指出|列出)'
                              r'|(?:直说|说清|明确|说明|指出|列出).{0,12}(?:不合适|不适合|不符合|不满足|排除(?:原因|依据))'
                              r'|(?:排除|不推荐).{0,8}(?:理由|原因|依据)')
_COLOUR_CONCLUSION = re.compile(
    r'^\s*(?:(?:但|但是|不过|然而|只是|可惜|而|且)\s*)?'
    r'(?:(?:它们?|(?:这|该)(?:款|些|两款|两个(?:选项)?|规格)|'
    r'(?:上述|以上)(?:两款|商品|候选|规格)?)(?:的颜色)?\s*)?'
    r'(?:颜色\s*)?(?:均|都|仍然|仍|却|并|也)?'
    r'(?:不符合|不满足|不能满足|达不到)(?P<target>[^。！？!?\n]+)[。！？!?\s]*$')


@dataclass
class Unit:
    parts: list = field(default_factory=list)
    ids: set = field(default_factory=set)
    variants: dict = field(default_factory=dict)
    independent: bool = False
    orphan: bool = False

    @property
    def text(self):
        return ''.join(self.parts)


def _normal(text):
    return re.sub(r'[\s，,、/／;；:：（）()。*`_-]', '', str(text)).replace('含充电盒', '含盒')


def _variant_map(scope, ids, text, has_variant):
    return {pid: {s['id'] for s in scope.resolve_skus(scope.ctx, scope.products[pid], text)}
            for pid in ids if has_variant(scope.products[pid], text)}


def _raw_bad(scope, unit):
    return {pid for pid in unit.ids if not scope.eligible.get(pid)
            or pid in unit.variants and not (scope.eligible[pid] & unit.variants[pid])}


def _condition_prefix(text):
    """Whether a conditional antecedent has no stated consequence yet."""
    start = _CONDITIONAL.match(text)
    if not start:
        return False
    remainder = text[start.end():]
    return not _CONSEQUENT.search(remainder)


def _bind_conditions(units, visible):
    for index, unit in enumerate(units):
        if unit.independent or unit.ids or not _condition_prefix(visible(unit.text)):
            continue
        following = units[index + 1] if index + 1 < len(units) else None
        # A condition immediately before a model offer belongs to that offer.
        # Deleting the offer therefore deletes its dependent antecedent too.
        adjoining = not re.search(r'[。！？!?]|\n\s*\n', unit.text)
        if following and following.ids and not following.independent and adjoining:
            following.parts = unit.parts + following.parts
        unit.parts = []
        unit.orphan = True
    return units


def _units(scope, text, *, visible, has_variant, negative, positive, service, bind_conditions=True):
    """Bound clauses by explicit subjects; punctuation alone is not an owner.

    New SKU dimensions begin their own information unit. Anonymous parameters,
    dates and conclusions stay with that unit until a new subject or an
    independent business/closing clause is present.
    """
    units = []
    current = None
    tokens = [part for token in re.findall(r'[^，,、。；;！？!?\n]+[，,、。；;！？!?\n]*|[，,、。；;！？!?\n]+', text)
              for part in re.split(r'(?=另外|此外|同时|以及|顺便)', token) if part]
    for token in tokens:
        part = visible(token)
        named = scope._ids(part)
        independent = bool(_FOOTER.search(part) and not named)
        service_clause = bool(service.search(part) or not named and scope.service_products and _REVIEW_RULE.search(part))
        conditional = bool(not named and not service_clause and _CONDITIONAL.match(part))
        # A service clause and its payment/status continuation are one unit;
        # a new recommendation of the same product is a distinct operation.
        new_service = service_clause and (current is None or not current.independent)
        if new_service:
            independent = True
        if (current and current.ids and not current.independent and not named
                and _CONCLUSION.search(part) and not _raw_bad(scope, current)):
            if not any(pid in scope.explicit for pid in current.ids):
                units.append(Unit([token], orphan=True))
                continue
        inherited = current.ids if current and not current.independent and not conditional else set()
        identities = named or inherited
        # In "black, does not meet the white requirement", white is the
        # requested condition, not a new offered variant. Keep the preceding
        # actual black owner and its rejection explanation together.
        colour_target = not named and _COLOUR_CONCLUSION.fullmatch(part.strip())
        variants = {} if colour_target else _variant_map(scope, identities, part, has_variant)
        different_variant = bool(current and any(pid in current.variants and options != current.variants[pid]
                                                  for pid, options in variants.items()))
        new_named = bool(named and (not current or named != current.ids
            or re.search(r'[。！？!?\n]\s*$', current.text)
            or current.independent and positive.search(negative.sub('', part))))
        boundary = current is None or independent or conditional or new_named or different_variant
        if boundary:
            current = Unit(ids=set(named or (inherited if different_variant else ())),
                           variants=variants, independent=independent)
            units.append(current)
        else:
            if named:
                current.ids = set(named)
            current.variants.update(variants)
        current.parts.append(token)
        # A blank line is a discourse boundary, unlike a simple list newline.
        if re.search(r'\n\s*\n\s*$', token):
            current = None
    return _bind_conditions(units, visible) if bind_conditions else units


def ground_future_prices(scope, text, *, visible, link, has_variant, negative, positive, service):
    """Current catalogue quotes never guarantee a future purchase price.

    Limit the correction to an attributed product's affirmative future money
    claim. Historical order amounts, questions, assumptions and qualifications
    stay intact, including a future re-quote after quantity/specification edits.
    No future price or logistics action is inferred from the current tool data.
    """
    if scope.ctx.get('catalog_evidence', {}).get('source') != 'fresh_database':
        return text
    links = []
    def mask(match):
        links.append(match.group())
        return '\ue000' + str(len(links) - 1) + '\ue001'
    def restore(value):
        return re.sub(r'\ue000(\d+)\ue001', lambda m: links[int(m[1])], value)
    def readable(value):
        return visible(restore(value))
    masked = link.sub(mask, text)
    units = _units(scope, masked, visible=readable, has_variant=has_variant,
                   negative=negative, positive=positive, service=service, bind_conditions=False)
    output = []
    for unit in units:
        if unit.independent or not unit.ids:
            output.append(restore(unit.text))
            continue
        for part in unit.parts:
            shown = readable(part)
            future = _FUTURE_QUOTE.search(shown)
            monetary = re.search(r'\d+(?:\.\d+)?\s*元|[¥￥]\s*\d+(?:\.\d+)?', shown)
            qualified = re.search(r'不能|无法|不保证|不确定|未确定|不一定|尚未|待核实|重新(?:核价|报价)|[？?]|吗', shown)
            hypothetical = re.search(r'假设|假定|假如|如果|若|要是', shown)
            guaranteed = re.search(r'必定|保证|一定|肯定|确定', shown)
            # An independent current amount followed by a future query in the
            # next clause must not lose its already verified current quotation.
            current_first = bool(future and monetary and monetary.start() < future.start()
                                 and re.search(r'当前|现在|本轮|今天|查询时', shown[:monetary.start()]))
            if not future or not monetary or qualified or current_first or hypothetical and not guaranteed:
                output.append(restore(part))
                continue
            ids = sorted(unit.ids)
            if len(ids) != 1:
                output.append(restore(part))
                continue
            product = scope.products[ids[0]]
            label = product.get('model') or product.get('name') or product['id']
            selected = unit.variants.get(product['id'], set())
            if len(selected) == 1:
                sku = next((s for s in product.get('skus', []) if s['id'] in selected), {})
                label += ' ' + str(sku.get('label') or sku.get('id') or '').strip()
            ending = '\n' if part.endswith('\n') else ''
            output.append(label + '补货后或未来下单的价格须届时重新核实，不能以本轮报价保证。' + ending)
    return ''.join(output)


def _candidates(scope, unit, pid):
    wanted = unit.variants.get(pid, scope.eligible.get(pid, set()))
    return [s for s in scope.products[pid].get('skus', []) if s['id'] in wanted & scope.eligible.get(pid, set())]


def _quotes(scope, sku):
    quantity = scope.quantities.get(sku['id'], 1)
    rows = [q for q in scope.current_quotes if q['sku_id'] == sku['id'] and q['quantity'] == quantity]
    if not rows:
        return None
    row = rows[-1]
    fields = ('unit_price_cents', 'subtotal_cents', 'discount_cents', 'total_cents')
    if any(type(row.get(key)) is not int or row[key] < 0 for key in fields):
        return None
    if (row['unit_price_cents'] * row['quantity'] != row['subtotal_cents']
            or row['subtotal_cents'] - row['discount_cents'] != row['total_cents']):
        return None
    return row


def _quote_gap(text):
    # A matching current quote settles this purchase quantity, not a future
    # order or a changed specification. Keep those conditional qualifications.
    return any((_PRICE_UNKNOWN.search(piece) or _REQUOTE.search(piece))
               and not _FUTURE_QUOTE.search(piece) for piece in re.split(r'[。！？!?\n]', text))


def _future_quote_notes(text):
    notes = []
    for piece in re.split(r'[。！？!?\n]', text):
        future = _FUTURE_QUOTE.search(piece)
        if future:
            note = piece[future.start():].strip('，,、；; ')
            if _PRICE_UNKNOWN.search(note) or _REQUOTE.search(note):
                notes.append(note + '。')
    return notes


def _current_keys(scope, unit, pid):
    options = _candidates(scope, unit, pid)
    if not options or not all(_quotes(scope, sku) for sku in options):
        return set()
    if scope.sku_requests:
        declared = {sku['id']: quantity for sku, quantity in
                    scope.sku_requests(scope.ctx, scope.products[pid], _STOCK_QUANTITY.sub('库存', unit.text))}
        if any(declared.get(sku['id']) != _quotes(scope, sku)['quantity'] for sku in options):
            return set()
    else:
        # The fallback has no application quantity resolver. Distinguish
        # stock counts from a quoted amount; unfamiliar counts stay unresolved.
        clean = _STOCK_QUANTITY.sub('', unit.text)
        counts = re.findall(r'([0-9]+|[零〇一二两三四五六七八九十百千万]+)\s*(?:件|副|套|个|双|台|只)', clean)
        numbers = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                   '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        quantities = [int(value) if value.isdigit() else numbers.get(value) for value in counts]
        if any(quantity != _quotes(scope, sku)['quantity'] for quantity in quantities for sku in options):
            return set()
    return {(pid, sku['id'], _quotes(scope, sku)['quantity']) for sku in options}


def _only_repeated_money_stock(scope, unit, pid, text, rendered):
    """Drop no new intent: only a duplicate of a block already reconstructed.

    Unknown, differently sized, conditional or separately requested quotes
    never enter this narrow grammar. Parameters, links and service also keep
    the unit outside it.
    """
    keys = _current_keys(scope, unit, pid)
    if not keys or not keys <= rendered or _FUTURE_QUOTE.search(text):
        return False
    product = scope.products[pid]
    labels = [pid, product.get('model'), product.get('name'), *product.get('aliases', [])]
    for sku in _candidates(scope, unit, pid):
        labels.extend([sku.get('id'), sku.get('label'), sku.get('color'), sku.get('size')])
    rest = text
    for label in sorted({str(value) for value in labels if value}, key=len, reverse=True):
        rest = re.sub(re.escape(label), '', rest, flags=re.I)
    rest = _REQUOTE.sub('', rest)
    rest = re.sub(r'(?:查询时)?(?:库存|现货)\s*(?:为|有)?\s*\d+\s*(?:件|台|副|个|套|只)?', '', rest)
    rest = re.sub(r'(?:单价|小计|优惠|实付|到手价|合计)\s*\d+(?:\.\d+)?\s*元', '', rest)
    rest = re.sub(r'(?:在|符合|满足)\s*\d+(?:\.\d+)?\s*元预算内', '', rest)
    for _, _, quantity in keys:
        rest = re.sub(r'(?:按)?\s*' + str(quantity) + r'\s*件(?:计算|报价)?\s*(?=[:：])', '', rest)
    return not re.sub(r'[\s，,、。；;:：（）()*`-]', '', rest)


def _needs_rebuild(scope, unit, pid, *, visible):
    """Verify an anonymous continuation; never infer an absent model name.

    A value recorded only for another product is a mismatch, not evidence that
    this fragment belongs to that other product. The original owner remains
    unknown and the explicit conforming block is rebuilt from its own facts.
    """
    if scope.ctx.get('catalog_evidence', {}).get('source') != 'fresh_database':
        return False
    product = scope.products[pid]
    options = _candidates(scope, unit, pid)
    if not options:
        return False
    text = visible(unit.text)
    specs = product.get('specs') if isinstance(product.get('specs'), dict) else {}
    corpus = _normal(' '.join(str(v) for v in specs.values()))
    known_phrases = {}
    for other in scope.products.values():
        for value in (other.get('specs') or {}).values():
            if not isinstance(value, (str, int, float)):
                continue
            for phrase in [str(value), *re.split(r'[，,、；;]', str(value))]:
                normalized = _normal(phrase)
                if len(normalized) >= 4:
                    known_phrases.setdefault(normalized, set()).add(other['id'])
    for part in unit.parts:
        clean = visible(part)
        # Named statements are handled by the existing factual guards. Here
        # the missing subject is the relevant boundary, including a fragment
        # that acquired a price/stock owner during an earlier numeric repair.
        technical = _normal(clean)
        matched = [phrase for phrase in known_phrases if phrase in technical]
        longest = [phrase for phrase in matched if not any(phrase != other and phrase in other for other in matched)]
        if any(pid not in known_phrases[phrase] for phrase in longest):
            return True
        for number, suffix in _NUMERIC_FIELD.findall(clean):
            value = _normal(number + suffix.lower())
            if value not in corpus.lower():
                return True
    if _quote_gap(text) and _current_keys(scope, unit, pid):
        return True
    counts = {s.get('stock') for s in options}
    if len(counts) == 1 and any(int(count) not in counts for count in _STOCK.findall(text)):
        return True
    if _RESTOCK.search(text) and all(type(s.get('stock')) is int and s['stock'] > 0 for s in options):
        # A current in-stock SKU has no pending-restock predicate. Never take
        # a sibling's date from the catalogue to make that predicate plausible.
        return True
    return False


def _render(scope, unit, pid):
    product = scope.products[pid]
    model = product.get('model') or product.get('name') or pid
    specs = product.get('specs') if isinstance(product.get('specs'), dict) else {}
    attributes = '、'.join(str(key) + '：' + str(value).strip() for key, value in specs.items()
                          if isinstance(value, (str, int, float)) and str(value).strip())
    items = conversation_state.effective_items({'items': scope.items}, product)
    limits = [i['value'] for i in items if i.get('key') == 'budget_max_cents'
              and type(i.get('value')) is int and i['value'] >= 0]
    lines = []
    for sku in _candidates(scope, unit, pid):
        price = _quotes(scope, sku)
        if price:
            fields = ('单价' + amounts.yuan(price['unit_price_cents']) + '元，优惠'
                      + amounts.yuan(price['discount_cents']) + '元，实付'
                      + amounts.yuan(price['total_cents']) + '元')
            if price['quantity'] != 1:
                fields = f"按{price['quantity']}件计算：" + fields
            if limits and price['total_cents'] <= min(limits):
                fields += '（在' + amounts.yuan(min(limits)) + '元预算内）'
        else:
            fields = '对应规格与数量报价尚未核实'
        pieces = [model + ' ' + sku.get('label', sku['id']) + '：' + fields]
        if attributes:
            pieces.append(attributes)
        pieces.append('查询时现货' + str(sku['stock']) + '件')
        lines.append('；'.join(pieces) + '。')
    return '\n'.join(lines)


def _requested_exclusions(scope, text, *, visible, negative):
    """Answer an explicit rejection-explanation request from current evidence.

    Do not list the whole shop for an ordinary recommendation. A current
    source-validated goal, scoped request or the explicit conforming display
    bounds the relevant category; unknown data is not an unsupported feature.
    """
    if not _EXPLAIN_EXCLUDED.search(scope.shopping_question):
        return text
    relevant = set(scope.relevant) if scope.has_scope else set()
    if not relevant:
        goal = scope.ctx.get('preferences', {}).get('shopping', {}).get('goal', {})
        users = {m.get('id'): m.get('content', '') for m in scope.ctx.get('messages', []) if m.get('role') == 'user'}
        category = goal.get('category')
        if (category and goal.get('source_message_id') in users
                and category in users[goal['source_message_id']]):
            relevant = {pid for pid, p in scope.products.items()
                        if category in p.get('name', '') or category in p.get('category', '')}
    if not relevant:
        displayed = scope._ids(visible(text))
        categories = {scope.products[pid].get('category') for pid in displayed if scope.eligible.get(pid)}
        relevant = {pid for pid, p in scope.products.items() if p.get('category') in categories}
    additions = []
    for pid, product in scope.products.items():
        if pid not in relevant or scope.eligible.get(pid):
            continue
        if any(pid in scope._ids(piece) and negative.search(visible(piece))
               for piece in re.split(r'[。\n]', text)):
            continue
        bad, unknown, evidence = [], [], []
        options = scope.resolve_skus(scope.ctx, product, scope.shopping_question)
        for sku in options:
            quantity = scope.quantities.get(sku['id'], 1)
            live = {q['sku_id']: q for q in scope.current_quotes if q['sku_id'] == sku['id'] and q['quantity'] == quantity}
            mismatch, pending = card_constraints._reasons(product, sku, scope.items, live)
            bad.extend(r['reason'] for r in mismatch)
            unknown.extend(r['reason'] for r in pending)
            evidence.extend(str(item['value']) for reason in mismatch for item in reason.get('catalogue_evidence', [])
                            if item.get('field') in (product.get('specs') or {}) and item.get('value') is not None)
            stock = sku.get('stock')
            if type(stock) is not int or stock < 0:
                unknown.append(sku.get('label', sku['id']) + '库存尚未核实')
            elif stock == 0:
                unknown.append(sku.get('label', sku['id']) + '当前库存0，缺货')
            elif type(quantity) is not int or quantity <= 0:
                unknown.append('购买或报价数量尚未核实')
            elif stock < quantity:
                unknown.append(sku.get('label', sku['id']) + '库存不足所需数量')
        reasons = list(dict.fromkeys([*bad, *unknown]))
        if not reasons:
            continue
        label = product.get('model') or product.get('name') or pid
        conclusion = '不符合当前条件' if bad else '尚未核实存在符合当前条件的现货规格'
        record = '；资料记录为“' + '；'.join(dict.fromkeys(evidence)) + '”' if evidence else ''
        additions.append(label + '：' + conclusion + '，不作为本次推荐；' + '；'.join(reasons) + record + '。')
    if not additions:
        return text
    return text.rstrip() + '\n\n' + '\n'.join(additions)


def _missing_feature_notes(scope, unit, *, visible, positive):
    """Keep an existing relevant unknown without keeping a rejected offer.

    The owner must be unambiguous and the unknown must refer to an accepted
    customer feature condition that the catalogue itself has not established.
    Preserve only the factual clause, never the proposed condition/purchase.
    """
    if len(unit.ids) != 1:
        return []
    pid = next(iter(unit.ids))
    product = scope.products[pid]
    requirements = [feature for item in conversation_state.effective_items({'items': scope.items}, product)
                    if item.get('key') in ('required_features', 'excluded_features')
                    for feature in item.get('value', []) if isinstance(feature, str) and feature]
    unknown = []
    for feature in requirements:
        status, evidence = card_constraints.feature_evidence(product, feature)
        if status is not None or evidence:
            continue
        # Use the same customer-feature aliases as the state layer, rather
        # than a phrase or component constant from a particular answer.
        labels = [feature, *(alias for alias, canonical in conversation_state._FEATURES.items() if canonical == feature)]
        unknown.extend(labels)
    if not unknown:
        return []
    notes = []
    for part in unit.parts:
        shown = visible(part)
        if (positive.search(shown) or not re.search(r'尚未(?:记录|明确|核实)|未(?:记录|记载|明确)|待核实|资料[^。\n]{0,8}不明确|无法确认|不能确认', shown)
                or not any(label in shown for label in unknown)):
            continue
        statement = part.strip(' \t\n，,、；;。')
        if not scope._ids(shown):
            statement = (product.get('model') or product.get('name') or pid) + '：' + statement
        notes.append(statement + '。')
    return notes


def _contradicted_colour_conclusion(scope, unit, referents, *, visible):
    """An anonymous colour-only rejection needs actual displayed SKU owners.

    Blank lines do not turn a verified white offer into a non-white one. Keep
    quotations, hypotheses, other requirements, mixed/unknown variants and
    genuine colour mismatches; customer-sourced current items are the only
    authority, never old assistant preferences. Precise shades remain precise.
    """
    if (unit.independent or not referents
            or scope.ctx.get('catalog_evidence', {}).get('source') != 'fresh_database'):
        return False
    statement = visible(unit.text).strip()
    match = _COLOUR_CONCLUSION.fullmatch(statement)
    if not match:
        return False
    target = match['target']
    colour_labels = set()
    for owner in referents:
        for pid in owner.ids:
            product = scope.products[pid]
            conditions = [i for i in conversation_state.effective_items({'items':scope.items},product)
                          if i.get('key') in ('colors','excluded_colors') and isinstance(i.get('value'),list)
                          and i['value']]
            # A general model reference or unknown colour does not establish
            # the colour of a displayed candidate. No variant is guessed here.
            options = owner.variants.get(pid)
            if not conditions or not options:
                return False
            skus = {s['id']:s for s in product.get('skus',[])}
            for sid in options:
                sku = skus.get(sid)
                if not sku or not sku.get('color'):
                    return False
                bad, unknown = card_constraints._reasons(product,sku,conditions,{})
                if bad or unknown:
                    return False
            for item in conditions:
                for value in item['value']:
                    if not isinstance(value,str) or not value:
                        return False
                    colour_labels.add(value)
                    colour_labels.update(shopping_state.COLOR_ALIASES.get(shopping_state.color_key(value),()))
    # The negative must be about the accepted colour constraint alone. A
    # feature, price, stock, precise shade, quotation or unknown subject is
    # substantive and cannot be erased as a colour-only contradiction.
    if not (any(label in target for label in colour_labels) or re.search(r'颜色|色调',target)):
        return False
    remainder = target
    for label in sorted(colour_labels,key=len,reverse=True):
        remainder = remainder.replace(label,'')
    remainder = re.sub(r'你|您|的|当前|现在|这次|本次|仍然|仍|坚持|保持|不变|颜色|色调|要求|条件|偏好', '',remainder)
    return not re.sub(r'[\s，,、：:]', '',remainder)


def prune(scope, text, *, visible, link, has_variant, negative, positive, service):
    # Keep URLs atomic while punctuation and ownership are inspected.
    links = []
    def mask(match):
        links.append(match.group())
        return '\ue000' + str(len(links) - 1) + '\ue001'
    masked = link.sub(mask, text)
    def restore(value):
        return re.sub(r'\ue000(\d+)\ue001', lambda m: links[int(m[1])], value)
    def readable(value):
        return visible(restore(value))
    units = _units(scope, masked, visible=readable, has_variant=has_variant,
                   negative=negative, positive=positive, service=service)
    output = []
    modified = False
    retained_ids = set()
    rendered_keys = set()
    colour_referents = []
    for unit in units:
        original = restore(unit.text)
        shown = readable(unit.text)
        if unit.orphan:
            modified = True
            continue
        if unit.independent:
            colour_referents = []
        elif unit.ids:
            # Keep every actual subject in the discourse window, including a
            # black comparison/exclusion. A mixed set cannot prove all-white.
            colour_referents.append(unit)
        if not unit.ids:
            if _contradicted_colour_conclusion(scope,unit,colour_referents,visible=readable):
                modified = True
                continue
            output.append(original)
            if shown.strip(' \t\n，,、。；;:：-*'):
                colour_referents = []
            continue
        # A negative explanation protects its complete subject and basis, even
        # when the conclusion followed punctuation or a list of features.
        negative_unit = bool(negative.search(shown) and not positive.search(negative.sub('', shown)))
        if negative_unit or unit.independent:
            output.append(original)
            retained_ids = unit.ids
            continue
        _, unsafe = scope._unsafe(shown, unit.ids)
        unsafe |= {pid for pid in _raw_bad(scope, unit) if not scope._protected(pid, shown)}
        if unit.ids == unsafe:
            output.extend(restore(note) for note in _missing_feature_notes(scope, unit, visible=readable, positive=positive))
            modified = True
            continue
        valid = unit.ids - unsafe
        explicit_protected = any(scope._protected(pid, shown) for pid in valid)
        if (not explicit_protected and len(valid) == 1
                and _only_repeated_money_stock(scope, unit, next(iter(valid)), shown, rendered_keys)):
            modified = True
            continue
        # A coordinated purchase alternative is separable from its discarded
        # member. Rebuild the remaining actual offer instead of retaining an
        # invitation to the nonconforming sibling with a contradictory footer.
        alternative_offer = bool(unsafe and len(valid) == 1 and
            scope.ctx.get('catalog_evidence', {}).get('source') == 'fresh_database'
            and re.search(r'或(?:者)?', shown) and re.search(r'可以|可选|购买|确认|推荐|建议选', shown)
            and not service.search(shown) and not negative.search(shown)
            and not re.search(r'比较|对比|差别|区别', shown))
        rebuild = bool(len(valid) == 1 and (alternative_offer or not explicit_protected and
                       _needs_rebuild(scope, unit, next(iter(valid)), visible=readable)))
        if rebuild:
            pid = next(iter(valid))
            rendered = _render(scope, unit, pid)
            if rendered:
                ending = '\n' if original.endswith('\n') else ''
                output.append(rendered + ''.join(_future_quote_notes(shown)) + ending)
                rendered_keys.update(_current_keys(scope, unit, pid))
                modified = True
                retained_ids = valid
                continue
        if unsafe:
            # Inseparable shared clauses retain the legal facts and explicitly
            # qualify the excluded member, rather than swallowing other intent.
            output.append(original)
            for pid in sorted(unsafe):
                label = scope.products[pid].get('model') or scope.products[pid].get('name') or pid
                output.append('（' + label + '尚未核实存在符合当前条件的现货规格，不作为本次推荐。）')
            modified = True
        elif unit.ids.isdisjoint(retained_ids) and not scope._ids(shown) and not unit.independent:
            # A surviving sibling after a deleted heading needs its real owner,
            # known from the original unit, stated instead of a dangling color.
            model = scope.products[next(iter(unit.ids))].get('model') or next(iter(unit.ids))
            output.append(model + ' ' + original.lstrip())
            modified = True
        else:
            output.append(original)
        retained_ids = valid
    result = ''.join(output)
    explained = _requested_exclusions(scope, result, visible=visible, negative=negative)
    modified |= explained != result
    result = explained
    if not modified:
        return text
    result = re.sub(r'(?m)^\s*[-*]\s*$', '', result)
    result = re.sub(r'[，,、]+(?=[。；;！？!?\n]|$)', '', result)
    result = re.sub(r'(?<=[。！？])(?:；|;|、|，|,)+', '', result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result
