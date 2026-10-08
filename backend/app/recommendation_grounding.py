"""Check recommendation claims against recorded attributes and current quotes."""
from __future__ import annotations

import re
from decimal import Decimal

from . import conversation_state, publication_state, conditional_constraints

UNKNOWN = r'尚未|未记录|未记载|不能确认|无法确认|不确定|是否|需核实|需要核实|待核实|未知'
CLAIM = r'(?:耳塞|耳套)?(?:材质)?(?:没有|不含|不带|非|无|不使用|不用|采用|使用|是|为|含有|配有)\s*(?:纯)?硅胶(?:材质)?(?:耳塞|耳套)?'


def _material_value(product):
    values = []
    for key, value in (product.get('specs') or {}).items():
        value = str(value)
        if (not re.search(r'耳塞|耳套|耳帽', key) or not re.search(r'材质|材料|成分', key)
                or '硅胶' not in value or re.search(UNKNOWN, value)):
            continue
        values.append(not bool(re.search(r'(?:无|非|不含|不使用|不是|没有)\s*(?:纯)?硅胶', value)))
    return values[0] if values and len(set(values)) == 1 else None


def earbud_material(text, ctx, mentioned_products):
    """Wearing style cannot establish whether an ear tip contains silicone.

    Keep preference quotations, questions and explicit unknowns. Only a
    product's own ear-tip material record can support a categorical claim;
    another model's attributes or a silicone case do not supply that evidence.
    """
    products = ctx.get('products', [])
    parts = re.split(r'(?<=[，,。！？；;\n])', text)
    preceding = []
    for index, part in enumerate(parts):
        named = mentioned_products(products, part)
        if named:
            preceding = named
        if '硅胶' not in part:
            continue
        if re.search(UNKNOWN, part):
            continue
        # Repeating the customer's preference is not a product assertion.
        claim = re.search(CLAIM, part)
        if not claim:
            continue
        subjects = named or preceding
        if not subjects:
            continue
        unsupported = [p for p in subjects if _material_value(p) is None]
        polarities = [_material_value(p) for p in subjects if _material_value(p) is not None]
        if not unsupported:
            asserted = not bool(re.search(r'没有|不含|不带|非|无|不使用|不用', claim[0]))
            if polarities and any(value != asserted for value in polarities):
                replacement = ('耳塞材质记录需按具体商品区分' if len(set(polarities)) > 1 else
                               ('耳塞材质记录为硅胶' if polarities[0] else '耳塞材质记录为非硅胶'))
                parts[index] = part[:claim.start()] + replacement + part[claim.end():]
            continue
        if re.search(r'符合|满足', part) and re.search(r'选项|要求|条件|需求', part):
            names = '、'.join(p.get('model') or p['id'] for p in unsupported)
            parts[index] = names + ' 的耳塞是否使用硅胶尚未记录，不能确认满足这项材质要求。\n'
        else:
            parts[index] = re.sub(CLAIM, '耳塞是否使用硅胶尚未记录', part)
    return ''.join(parts)


def shortlist_headings(text, ctx, facts, mentioned_products, requested_skus):
    """A shortlist heading must not call an over-budget bullet a matching pick."""
    preferences = [item for item in [*ctx.get('preferences', {}).get('items', []), *conditional_constraints.items(ctx)]
                   if item.get('key') == 'budget_max_cents']
    question = next((m.get('content', '') for m in reversed(ctx.get('messages', []))
                     if m.get('role') == 'user'), '')
    explicit = re.search(r'预算(?:改到|上限|是|为)?\s*(\d+(?:\.\d+)?)\s*元|(\d+(?:\.\d+)?)\s*元(?:内|以内|以下)', question)
    quotes = [f['result'] for f in facts if f.get('tool') == 'quote_price'
              and isinstance(f.get('result'), dict)
              and type(f['result'].get('total_cents')) is int]
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        positive = re.search(r'(?:符合的(?:有|是)|(?:这|以下)?[两二三四\d]+款(?:都)?(?:合适|符合|满足))', line)
        if not positive or not re.search(r'[：:]\s*$', line):
            continue
        named_bullets = []
        for following in lines[index + 1:]:
            if not following.strip():
                continue
            if not re.match(r'^\s*[-*+]\s+', following):
                break
            for product in mentioned_products(ctx.get('products', []), following):
                applicable = [item for item in preferences
                              if item.get('scope') in (None, '', '全部', 'all', '会话', product.get('category'))
                              and item.get('product_id') in (None, product['id'])]
                limits = [Decimal(str(item['value'])) for item in applicable
                          if item.get('active') and type(item.get('value')) in (int, float)]
                ceiling = min(limits) if limits else (Decimal(explicit[1] or explicit[2]) * 100
                            if explicit and not applicable and not re.search(r'取消|撤销|不再', question) else None)
                if ceiling is None:
                    if re.search(r'预算|\d+(?:\.\d+)?\s*元(?:内|以内|以下)', line):
                        named_bullets.append(False)
                    continue
                skus = requested_skus(ctx, product, following)
                counts = [int(m[1]) for m in re.finditer(r'(\d+)\s*(?:件|副|个|套)', following)
                          if not re.search(r'库存|现货|剩|有货', following[max(0, m.start()-10):m.start()])]
                quantity = counts[0] if counts and len(set(counts)) == 1 else 1 if not counts else None
                matched = [q for q in quotes if q.get('sku_id') in {s['id'] for s in skus}
                           and q.get('quantity') == quantity]
                named_bullets.append(bool(matched) and all(q['total_cents'] <= ceiling for q in matched))
        if named_bullets and not all(named_bullets):
            lines[index] = '按你当前有效条件，各款核对结果如下（超预算或尚未核价的款式不作为预算内推荐）：\n'
    return ''.join(lines)


_BUDGET_ASSERTION = re.compile(
    r'(?:在|属于)?\s*\d+(?:\.\d+)?\s*元预算(?:内|外)|预算(?:内|外|关系)'
    r'|(?:未设置|没有设置|未设|不设)[^，,。；;\n]{0,8}预算(?:上限|限制|约束)?'
    r'|(?:不按|不受)[^，,。；;\n]{0,8}预算(?:限制|约束|上限|要求)(?:判断)?'
    r'|(?:符合|满足|超出|超过|低于|高于|不超过|没有|无|不足|不够)[^，,。；;\n]{0,30}预算'
    r'|\d+(?:\.\d+)?\s*元(?:以内|以下|内)[^，,。；;\n]{0,24}(?:选择|可买|可购|选购|推荐|有|是)')
_LEADING_BUDGET_RANGE = re.compile(
    r'^\s*(?:(?:[-*+]\s+|\d+[.)、]\s*))?\*{0,2}'
    r'\d+(?:\.\d+)?\s*元(?:以内|以下|内)[^，,：:。！？；;\n]{0,40}[，,：:]')
_BUDGET_RANGE_START = re.compile(
    r'^\s*(?:(?:[-*+]\s+|\d+[.)、]\s*))?\*{0,2}'
    r'\d+(?:\.\d+)?\s*元(?:以内|以下|内)')


def _inline_budget_prefix(unit, products, mentioned_products):
    """A leading price-range noun phrase qualifies its immediate candidates.

    A customer's condition recap, a negative search result, and an unrelated
    service topic are not candidate assertions. The boundary stays within one
    sentence and the first following clause must actually name a product.
    """
    beginning = _BUDGET_RANGE_START.match(unit)
    if not beginning:
        return ''
    match = _LEADING_BUDGET_RANGE.match(unit)
    first_clause = re.split(r'[，,：:。！？；;\n]', unit, 1)[0]
    named = mentioned_products(products, first_clause)
    prefix = match[0] if match else ''
    if named:
        # The noun phrase may meet the product name directly, without a
        # comma: "100元以内的随行杯R8..." is still a range assertion.
        starts = [m.start() for product in named for alias in
                  [product.get('name'), product.get('model'), product.get('id'), *product.get('aliases', [])]
                  if alias for m in re.finditer(
                      re.escape(str(alias)) + (r'(?![A-Za-z0-9])' if str(alias)[-1].isascii() and str(alias)[-1].isalnum() else ''),
                      first_clause, re.I) if m.start() >= beginning.end()]
        if not starts:
            return ''
        prefix = unit[:min(starts)]
    if (not prefix or len(prefix) - beginning.end() > 40
            or re.search(r'你|您|条件|要求|预算|没有|暂无|尚无|找不到|无法|不符合|不满足|订单|售后', prefix)):
        return ''
    following = re.split(r'[，,。！？；;\n]', unit[len(prefix):], 1)[0]
    if (not mentioned_products(products, following)
            or re.search(r'订单|售后|退货|退款|物流|运单|维修', following)):
        return ''
    return prefix


def _remove_unmatched_closing_parentheses(text):
    # Only used inside a budget scope already being reconstructed. Keep real
    # feature brackets and Markdown link targets; discard orphaned closers.
    output, opened = [], []
    pairs = {')': '(', '）': '（'}
    for char in text:
        if char in ('(', '（'):
            opened.append(char)
        elif char in pairs:
            if not opened or opened[-1] != pairs[char]:
                continue
            opened.pop()
        output.append(char)
    return ''.join(output)


def _budget_limit(ctx, product, question):
    """Only the customer's effective category/product limit is authoritative."""
    all_items = [*ctx.get('preferences', {}).get('items', []), *conditional_constraints.items(ctx)]
    applicable = [item for item in all_items if item.get('key') == 'budget_max_cents'
                  and item.get('scope') in (None, '', 'all', '全部', '会话', product.get('category'))
                  and item.get('product_id') in (None, product['id'])]
    values = [Decimal(str(item['value'])) for item in applicable
              if item.get('active') is True and type(item.get('value')) in (int, float)
              and Decimal(str(item['value'])).is_finite() and item['value'] >= 0]
    if values:
        return min(values)
    if applicable or any(item.get('key') == 'budget_max_cents' for item in all_items):
        # Recorded scope is authoritative. A withdrawal cannot revive an old
        # number, nor can a different category's limit become a global one
        # merely because the current message also mentions another product.
        return None
    # Pure helper callers may not have ingested state yet. Parse only the
    # current customer's assertion; never take a limit from generated prose.
    changes = conversation_state.changes_from_text(question, ctx.get('products', []))
    scope = conversation_state._scope(question, ctx.get('products', []), None)
    value = changes.get('budget_max_cents')
    return Decimal(value) if type(value) is int and scope in ('会话', product.get('category')) else None


def budget_statements(text, ctx, facts, mentioned_products, requested_skus, *,
                      price_claim_requests, normalized_price_quote,
                      noncurrent_price_scope, scoped_price_quotes):
    """Render current budget verdicts from the same SKU/count/quote evidence.

    This owns whole budget assertions, not money fields. Factual price and
    feature clauses survive; past payments and hypothetical examples do not
    acquire today's budget. Existing headline-only callers remain unchanged.
    """
    products = ctx.get('products', [])
    question = next((m.get('content', '') for m in reversed(ctx.get('messages', []))
                     if m.get('role') == 'user'), '')
    if (re.search(r'如果|假如|假设|倘若|要是|上次|历史|以前', question)
            and not re.search(r'现在|当前|本次|改买|取消|撤销|改到|改为', question)):
        return text
    quotes = [quote for fact in facts if fact.get('tool') == 'quote_price'
              and isinstance(fact.get('result'), dict) and not fact['result'].get('error')
              if (quote := normalized_price_quote(fact['result']))]

    def amount(cents):
        return format(Decimal(cents) / 100, 'f').rstrip('0').rstrip('.') if cents % 100 else str(cents // 100)

    def verdict(product, scope):
        name = product.get('model') or product.get('name') or product['id']
        limit = _budget_limit(ctx, product, question)
        if limit is None:
            return f'{name} 本次未设置预算上限，不按旧预算限制判断。', 'unrestricted'
        ceiling = format(limit / 100, 'f').rstrip('0').rstrip('.') if limit % 100 else str(int(limit // 100))
        customer_skus = {s['id'] for s in requested_skus(ctx, product, question)}
        requests = [(s, q) for s, q in price_claim_requests(ctx, product, question, scope, quotes)
                    if s['id'] in customer_skus and type(q) is int and q > 0]
        matched = scoped_price_quotes(ctx, question, product, scope, quotes)
        rows = []
        for sku, quantity in requests:
            candidates = [q for q in matched if q['sku_id'] == sku['id'] and q['quantity'] == quantity]
            totals = {q['total_cents'] for q in candidates}
            if len(totals) != 1:
                return f'{name} 的对应规格与数量报价尚未核实，不能确认是否满足{ceiling}元预算。', 'unknown'
            paid = next(iter(totals))
            conclusion = (f'在{ceiling}元预算内' if paid <= limit
                          else f'超过{ceiling}元预算（超出{amount(paid - limit)}元），不作为预算内选项')
            rows.append(f'{name} {sku.get("label", sku["id"])} {quantity}件：实付{amount(paid)}元，{conclusion}。')
        if rows:
            status = 'within' if all(q['total_cents'] <= limit for q in matched) else 'over'
            return ' '.join(dict.fromkeys(rows)), status
        return f'{name} 的对应规格与数量报价尚未核实，不能确认是否满足{ceiling}元预算。', 'unknown'

    # A constrained collection is an assertion about all displayed members,
    # whether its entries are bold paragraphs, prose, numbered or bulleted.
    # Use the same SKU/count/quote verdict as individual budget statements.
    replacements = []
    for group in publication_state.displayed_product_groups(text, products, mentioned_products):
        heading = group['heading']
        if not heading or noncurrent_price_scope(heading['text']):
            continue
        claim = heading['text']
        if not re.search(r'可选|选择|可买|可购|推荐|符合|满足|合适|候选', claim):
            continue
        entries = [(p, entry['text']) for entry in group['entries'] for p in entry['products']]
        constrained = any(_budget_limit(ctx, p, question) is not None for p, _ in entries)
        if not constrained or not (re.search(r'预算|按.{0,12}(?:条件|要求)|符合|满足', claim)
                                   or re.search(r'可选|推荐|合适|候选', claim)):
            continue
        outcomes = [verdict(p, scope)[1] for p, scope in entries]
        if any(status in ('over', 'unknown') for status in outcomes):
            replacements.append((heading['start'], heading['end'],
                '按你当前有效条件，各款核对结果如下（超预算或尚未核价的款式不作为预算内推荐）：\n'))
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]

    units = []
    for unit in re.split(r'(?<=[。！？；;\n])', text):
        # A later conditional branch cannot exempt a preceding independent
        # current budget assertion in the same sentence.
        conditional = re.search(r'[，,]\s*(?:如果|假如|假设|倘若|要是)', unit)
        if conditional and not noncurrent_price_scope(unit[:conditional.start()]):
            units.extend((unit[:conditional.start()] + '。', unit[conditional.start() + 1:]))
            continue
        if _inline_budget_prefix(unit, products, mentioned_products):
            boundary = re.search(r'[，,]\s*(?:(?:关于|至于|另外|还有)\s*)?'
                                 r'(?:订单|售后|退货|退款|物流|运单|维修|申请)', unit)
            if boundary:
                # The independent order clause must neither exempt the
                # preceding shopping claim nor be rewritten by its budget.
                units.extend((unit[:boundary.start()] + '。', unit[boundary.start() + 1:]))
                continue
        units.append(unit)
    output = []
    preceding = []
    preceding_scope = ''
    historical_heading = False
    for index, unit in enumerate(units):
        if not unit.strip():
            historical_heading = False
            preceding = []
            preceding_scope = ''
            output.append(unit)
            continue
        range_prefix = _LEADING_BUDGET_RANGE.match(unit)
        if (range_prefix and re.search(r'(?:你|您)(?:的)?(?:条件|要求|预算)', range_prefix[0])
                and not _BUDGET_ASSERTION.search(unit[range_prefix.end():])):
            output.append(unit)  # Merely repeating the customer's requirement.
            continue
        named = mentioned_products(products, unit)
        if noncurrent_price_scope(unit):
            historical_heading = bool(re.search(r'[：:]\s*$', unit))
            output.append(unit)
            continue
        if historical_heading:
            output.append(unit)
            continue
        if named:
            preceding, preceding_scope = named, unit
        inline_prefix = _inline_budget_prefix(unit, products, mentioned_products)
        if not _BUDGET_ASSERTION.search(unit) and not inline_prefix:
            output.append(unit)
            continue
        subjects = named or preceding
        scopes = {p['id']: (unit if named else preceding_scope + ' ' + unit) for p in subjects}
        if not subjects and re.search(r'[：:]\s*$', unit):
            # A positive heading inherits only its contiguous shortlist,
            # never an unrelated product mentioned later in the conversation.
            for following in units[index + 1:]:
                if not following.strip():
                    continue
                if not re.match(r'\s*[-*+]\s+', following):
                    break
                for product in mentioned_products(products, following):
                    if product not in subjects:
                        subjects.append(product)
                    scopes[product['id']] = following
        if not subjects:
            output.append(unit)
            continue
        # Keep non-budget clauses, including correct money fields and product
        # attributes. Reconstruct the budget verdict as a complete sentence;
        # no fragment is replaced by another fragment inside a noun phrase.
        outcomes = [verdict(p, scopes[p['id']]) for p in subjects]
        rendered = ' '.join(result for result, _ in outcomes)
        if unit.strip() == rendered:
            output.append(unit)
            continue
        retained = []
        factual_unit = unit[len(inline_prefix):] if inline_prefix else unit
        if inline_prefix:
            if inline_prefix.count('**') % 2:
                factual_unit = factual_unit.replace('**', '', 1)
            factual_unit = _remove_unmatched_closing_parentheses(factual_unit)
        for clause in re.split(r'[，,]', factual_unit.strip()):
            assertion = _BUDGET_ASSERTION.search(clause)
            if assertion:
                # The assertion can follow a real feature without a comma:
                # keep that subject/feature prefix and any separately joined
                # factual suffix, but discard the entire budget predicate.
                prefix = re.sub(r'(?:并且|而且|同时|并|且|也|还|是|在|属于)\s*$', '', clause[:assertion.start()]).strip()
                if re.search(r'未设置|没有设置|未设|不设|不按|不受', assertion[0]):
                    prefix = re.sub(r'(?:本次|当前|现在|目前|仍然|仍|已经|已|原先|之前)\s*$', '', prefix).strip()
                subject_only = any(prefix in {str(alias).strip() for alias in
                    [p.get('id', ''), p.get('model', ''), p.get('name', ''), *p.get('aliases', [])]} for p in subjects)
                if mentioned_products(products, prefix) and not subject_only:
                    retained.append(prefix)
                suffix = clause[assertion.end():]
                joined = re.search(r'(?:并且|而且|同时|并|且)(.+)', suffix)
                if joined and not _BUDGET_ASSERTION.search(joined[1]):
                    retained.append(joined[1].rstrip('。；;：:\n'))
                continue
            if re.fullmatch(r'\s*(?:按|根据)?[^。]{0,18}预算[^。]{0,16}[：:]?\s*', clause):
                continue
            # A budget difference belongs to the same verified total/limit
            # calculation. Do not retain a writer's conflicting delta next
            # to the recomputed result (or any delta when price is unknown).
            if re.fullmatch(r'\s*(?:也|还|仍|实际|共)?(?:超出|超过|多出)\s*\d+(?:\.\d+)?\s*元[。；;]?\s*', clause):
                continue
            if (any(status in ('over', 'unknown') for _, status in outcomes)
                    and re.fullmatch(r'\s*(?:所以|因此)?(?:可以|可|建议)(?:购买|买|下单)[。；;]?\s*', clause)):
                continue
            if clause.strip(' 。；;：:\n') and not re.fullmatch(r'\s*(?:本店|店内)[^，,。]{0,12}(?:里|中)\s*', clause):
                retained.append(clause.rstrip('。；;：:\n'))
        factual = '，'.join(retained).strip()
        output.append((factual + '。' if factual else '') + rendered + ('\n' if unit.endswith('\n') else ''))
    return ''.join(output)
