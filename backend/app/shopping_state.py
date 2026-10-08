"""Small customer-sourced shopping references, distinct from information topics."""
import copy
import re


COLOR_ALIASES = {'白':('白色','暖白','米白'), '黑':('黑色','曜石黑','曜黑'),
                 '米':('米色','燕麦色'), '绿':('绿色','鼠尾草绿','雾绿')}
COLOR_WORDS = tuple(dict.fromkeys(word for aliases in COLOR_ALIASES.values() for word in aliases))
COLORS = '|'.join(re.escape(word) for word in sorted(COLOR_WORDS,key=len,reverse=True))
SIZE = r'(?<![A-Za-z0-9])(?:XXL|XL|XS|S|M|L)(?![A-Za-z0-9])'
CATEGORIES = r'耳机|鼠标|键盘|水壶|风扇|衣服|短袖|衬衫|外套|杯子|水杯|毛巾'
GOAL_ACTION = r'我选|想买|需要|想要|选购|用于|用来|预算|必须|推荐|考虑'
COUNT = r'([1-9][0-9]?|[一二两三四五六七八九十])\s*(?:件|副|套|台|双|个)(?!颜色|条件|要求|规格|事|问题|诉求|信息|疑问)'


def _query_colour_pattern(products):
    words = {*COLOR_WORDS, '纯白', '冷白'}
    words.update(str(sku['color']) for product in products for sku in product.get('skus', []) if sku.get('color'))
    return '|'.join(re.escape(word) for word in sorted(words, key=len, reverse=True))


def _current_variant_price_query(text, products):
    # An information query does not establish a purchase or a lasting colour
    # preference. Historical payments and conditional examples stay separate.
    if re.search(r'订单|这笔|售后|退货|退款|换货|维修|如果|假如|假设|若|朋友|别人|帮[他她]|替[他她]|'
                 r'之前|以前|过去|上次|曾经|历史|原价|明天|将来|未来|补货后|届时', text):
        return False
    if re.search(r'(?:不要|(?<!分)别|不必|不用|无须|无需|不再|不想|不)(?:再)?(?:查|查询|核对|报价|询价|问|报)', text):
        return False
    variants = list(re.finditer(_query_colour_pattern(products) + '|' + COUNT + '|' + SIZE, text, re.I))
    prices = list(re.finditer(r'单价|售价|现价|价格|多少钱|报价|实付', text))
    # A preceding product's price is not the following product's colour query
    # (e.g. "A's price and B white's capacity").
    return any(variant.end() <= price.start() for variant in variants for price in prices)


def _shared_variant_queries(text, products):
    aliases = {}
    for product in products:
        for alias in [product['id'], product.get('model'), product.get('name'), *product.get('aliases', [])]:
            if alias: aliases.setdefault(str(alias).lower(), set()).add(product['id'])
    result = {}
    for clause in re.split(r'[，,。；;！？?\n]', text):
        if not _current_variant_price_query(clause, products): continue
        candidates = []
        for alias, ids in aliases.items():
            if len(ids) != 1: continue
            pattern = r'(?<![A-Za-z0-9_-])' + re.escape(alias) + r'(?![A-Za-z0-9_-])' if alias.isascii() else re.escape(alias)
            candidates.extend((m.start(), m.end(), next(iter(ids))) for m in re.finditer(pattern, clause, re.I))
        spans = []
        for start, end, pid in sorted(candidates, key=lambda item: -(item[1] - item[0])):
            if not any(start < b and end > a for a, b, _ in spans): spans.append((start, end, pid))
        spans.sort()
        index = 0
        while index < len(spans):
            last = index
            while last + 1 < len(spans) and re.fullmatch(r'\s*(?:和|与|以及|及|、)\s*', clause[spans[last][1]:spans[last + 1][0]]):
                last += 1
            group = spans[index:last + 1]
            if len({pid for _, _, pid in group}) > 1:
                tail_end = spans[last + 1][0] if last + 1 < len(spans) else len(clause)
                tail = clause[group[-1][1]:tail_end]
                colour = re.match(r'\s*的?\s*(' + _query_colour_pattern(products) + r')', tail)
                if colour and not re.search(r'不要|不选|不买|排除|不考虑', tail):
                    prefix = clause[:group[0][0]]
                    for start, end, pid in group:
                        record = {'text': prefix + clause[start:end] + tail, 'color': colour.group(1), 'source_text': clause}
                        if pid in result and result[pid] != record: result[pid] = None
                        elif pid not in result: result[pid] = record
            index = last + 1
    return result


def shared_variant_query_text(text, product_id, products):
    """Project only an explicit coordinated current-price colour qualifier.

    Private variants and historical/conditional clauses use the caller's normal
    subject handling. The projection neither mutates state nor selects a SKU.
    """
    record = _shared_variant_queries(text, products).get(product_id)
    return record['text'] if record else None


def color_key(value):
    for key, aliases in COLOR_ALIASES.items():
        if value in aliases: return key
    return value


def positive_corrections(text):
    """A denied change cannot displace an earlier explicit current variant."""
    pattern = r'(?:现在|目前|这次|本次)?(?:更改|修改|改|换)(?:买|成|为|到|穿|看|选)?'
    matches = []
    for match in re.finditer(pattern,text):
        before = re.split(r'[，,。；;！？?\n]|但是|不过|另外',text[:match.start()])[-1]
        if re.search(r'(?:不|没|未|别|不要|不想|不愿意|不需要|不必|不用|不再|无需|无须|'
                     r'取消|停止|拒绝|不打算|不是(?:要|想))'
                     r'(?:再|去|要|想|会|能|可以|必须)?\s*$',before):
            continue
        matches.append(match)
    return matches


def _purchase_question(text):
    return bool(re.search(r'(?:怎么|如何|是否|能否|能不能|可不可以|可否).{0,12}买|(?:可以|能)买|买.{0,12}(?:吗|么|行不行|行吗|可以吗|过了)', text))


def _choice_declaration(text):
    pattern = r'(?:我|本人)(?:现在|这次)?(?:决定买|确定买|要买|想买|选择|选定|选|要)|(?:现在|这次)(?:改买|改成|改为|换成|换为)'
    return any(re.match(r'[^，,。；;！？?\s]', text[m.end():].lstrip()) for m in re.finditer(pattern, text))


def _past_purchase(text):
    return bool(re.search(r'(?:之前|以前|曾经|原来|上次|早先|已经|已).{0,12}买|买(?:过|了)', text))


def current_request_text(text):
    """Mask a declined shopping task without erasing its affirmative follow-up.

    Keeping offsets and the unmodified customer message preserves provenance;
    only current selection/entry inference consumes this projection.
    """
    # A quoted shopping utterance is source text, not a current instruction.
    # Product-name quotation in "我选‘E1暖白’" contains no shopping action and
    # remains available to the customer's affirmative surrounding request.
    quoted = r'“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"'
    text = re.sub(quoted, lambda m: ' ' * len(m[0])
                  if re.search(GOAL_ACTION, m[0]) and re.search(CATEGORIES, m[0])
                  else m[0], text)
    spans = []
    for match in re.finditer(r'[^，,。；;！？?\n]+', text):
        # A contrasting affirmative request starts its own scope even when
        # the customer omits punctuation between the two requests.
        for part in re.finditer(r'(?:^|而是|但是|不过|但|另外|同时|并且)(.*?)(?=而是|但是|不过|但|另外|同时|并且|$)', match.group()):
            clause = part[1]
            denied = re.match(
                r'^\s*(?:我)?(?:'
                r'(?:不是|并非)(?:让|要|想让|要让)(?:你|您)?(?:再)?(?:推荐|帮我选|挑一)|'
                r'(?:不用|不必|不需要|不要|别)(?:再)?(?:帮我|给我|为我)?(?:推荐|帮我选|挑一)|'
                r'(?:不需要|不想要|不考虑)(?!不))', clause)
            reported = re.match(r'^\s*(?:他|她|你|您)(?:刚才|之前|曾经)?(?:说|提到|表示|要求)', clause)
            if reported or denied and (re.search(CATEGORIES, clause) or re.search(r'推荐|帮我选|挑一', denied[0])):
                start = match.start() + part.start(1)
                spans.append((start, match.start() + part.end(1)))
    for start, end in reversed(spans):
        text = text[:start] + ' ' * (end - start) + text[end:]
    return text


def _shopping_segments(text, products, find_products):
    """Keep source substrings whose action is shopping, not a nearby order.

    A shared purchase/quote verb can govern comma-separated specifications.
    A new service, information, historical or hypothetical clause ends that
    inheritance; its quantity/category cannot enter the shopping reference.
    """
    boundaries = list(re.finditer(
        r'[，,。；;！？?\n]+|另外|此外|顺便|同时|(?=(?:旧|另)?订单|这(?:一)?单|这笔订单)', text))
    spans = []
    start = 0
    for boundary in boundaries:
        if boundary.start() > start: spans.append((start, boundary.start()))
        start = boundary.end()
    if start < len(text): spans.append((start, len(text)))
    groups, carried, adjoining = [], None, False
    for start, end in spans:
        part = text[start:end]
        named = find_products(part, products)
        service = re.search(r'订单|这(?:一)?单|这笔|售后|退货|退款|换货|补发|保修|维修|物流|签收|收货|退.{0,16}(?:件|副|台|套|双|个)|转人工|人工(?:接管|客服|处理|帮忙)|交接|带过去|带上.{0,12}(?:信息|事情|问题|诉求)', part)
        hypothetical = re.search(r'如果|假如|假设|若(?:我|自己|本人|将来|以后|未来|要|买|想|不能)|否则', part)
        other_person = re.search(r'朋友|帮[他她]|替[他她]', part)
        factual = re.search(r'洗护|护理|材质|容量|参数|有哪些|有什么|介绍|说说|告诉我', part)
        declared = _choice_declaration(part)
        historical = _past_purchase(part) and not declared
        cancellation = re.search(r'不要(?:再)?(?:买|选)|不买了|取消选购|取消购买', part)
        explicit = re.search(r'决定买|改买|改成|改为|换成|换为|改穿|改看|换看|选定|选择|我选|我要|我想买|想要|需要|最初要|买', part)
        explicit = explicit or re.search(r'数量(?:改|改为|改成|调整到)\s*' + COUNT, part)
        explicit = explicit or re.search(r'(?:我|本人)(?:只|仅)?穿\s*'+SIZE, part, re.I)
        quote = named and ((re.search(r'分别查|分别报|各自报价|多少钱|报价|实付', part) and re.search(COUNT, part))
                           or _current_variant_price_query(part, products))
        information_action = re.search(r'比较|对比|咨询|了解|说明|解释', part)
        direct_choice = re.search(r'决定买|确定买|要买|想买|改买|改成|改为|换成|换为|选定|选择|我选', part)
        # Asking whether to purchase and narrating an earlier purchase do not
        # establish a present choice, even if another clause chose an item.
        excluded_goal = re.search(r'(?:别|不要|不必|不用|不再|不)(?:再)?(?:向我|给我)?(?:推荐|考虑|选购)', part)
        not_current = hypothetical or historical or other_person or excluded_goal or (_purchase_question(part) and not declared)
        if service or not_current or (factual and not declared and not quote) or (information_action and not direct_choice and not quote):
            carried, adjoining = None, False
            continue
        if cancellation:
            kind = 'cancellation'
        elif explicit:
            kind = 'explicit_choice'
        elif quote:
            kind = 'quote_request'
        elif re.search(CATEGORIES, part) and re.search(GOAL_ACTION, part):
            kind = 'goal'
        elif carried and (not named or re.search(COLORS+'|'+SIZE+'|'+COUNT, part, re.I)):
            kind = carried
        else:
            carried, adjoining = None, False
            continue
        # A separate information quote must not inherit the purchase kind;
        # an explicit buy+price request still represents the stated choice.
        if quote and not declared and not re.search(r'改买|改成|改为|换成|换为', part):
            kind = 'quote_request'
        correction = named and re.search(r'改买|改成|改为|换成|换为', part)
        if adjoining and groups[-1][2] == kind and not correction:
            groups[-1] = (groups[-1][0], end, kind)
        else:
            groups.append((start, end, kind))
        carried = kind if kind in ('explicit_choice', 'quote_request') else None
        adjoining = bool(carried)
    return [(text[start:end], kind) for start, end, kind in groups]


def update(previous, text, products, source, find_products):
    text = current_request_text(text)
    state = copy.deepcopy(previous or {'selections': [], 'goal': None})
    collected = {}
    # Keep the existing conditional-alternative boundary before clause splitting;
    # a following '就买…' must not lose its preceding '若不能'.
    text = re.split(r'若不能|否则|如果不行|(?:不行|不能)(?:的话|就)|不能.{0,5}时', text, maxsplit=1)[0]
    for segment, kind in _shopping_segments(text, products, find_products):
        before = state.get('selections', [])
        changed = _update_segment(state, segment, products, source, find_products, kind)
        if changed.get('selections', []) != before:
            if kind == 'cancellation':
                allowed = {item['product_id'] for item in changed.get('selections', [])}
                collected = {pid:item for pid,item in collected.items() if pid in allowed}
            else:
                if find_products(segment, products) and re.search(r'改买|改成|改为|换成|换为', segment):
                    collected = {}
                collected.update({item['product_id']:item for item in changed.get('selections', [])})
                changed['selections'] = list(collected.values())
        state = changed
    return state


def _update_segment(previous, text, products, source, find_products, kind):
    state = copy.deepcopy(previous or {'selections': [], 'goal': None})
    # A conditional fallback specifies an option to discuss, not a committed
    # shopping selection. Keep any independently stated primary selection.
    text = re.split(r'若不能|否则|如果不行|(?:不行|不能)(?:的话|就)|不能.{0,5}时', text, maxsplit=1)[0]
    named = find_products(text, products)
    # Questions about properties do not mean "choose this product".
    factual = bool(re.search(r'洗护|护理|材质|容量|参数|有哪些|有什么|介绍|说说|告诉我', text))
    explicit = bool(re.search(r'决定买|改买|改成|改为|换成|换为|改穿|改看|换看|选定|选择|我选|我要|我想买|想要|需要|最初要|买', text))
    explicit = explicit or bool(re.search(r'数量(?:改|改为|改成|调整到)\s*' + COUNT, text))
    explicit = explicit or bool(re.search(r'(?:我|本人)(?:只|仅)?穿\s*' + SIZE, text, re.I))
    if (_purchase_question(text) or _past_purchase(text)) and not _choice_declaration(text): explicit = False
    explicit = explicit or kind == 'explicit_choice'
    quote_items = bool(named and kind == 'quote_request')
    if quote_items: explicit = False
    if re.search(r'不要(?:再)?(?:买|选)|不买了|取消选购|取消购买', text) and not re.search(r'现在|改买|改成|改为|换成', text):
        state['selections'] = [x for x in state.get('selections', []) if named and x['product_id'] not in named]
        state['source_message_id'] = source
        return state
    if named and (quote_items or (explicit and not (factual and not re.search(r'决定买|改买|改成|改为|换成|换为|我要买|我想买', text)))):
        # A new definite choice replaces the prior choice. A single multi-item
        # request preserves its individual SKU and quantity relationships.
        candidates = []
        for pid in named:
            product = next(p for p in products if p['id'] == pid)
            aliases = [pid, product.get('model', ''), product.get('name', ''), *product.get('aliases', [])]
            for alias in aliases:
                if not alias: continue
                pattern = r'(?<![A-Za-z0-9])'+re.escape(alias)+r'(?![A-Za-z0-9])' if alias.isascii() else re.escape(alias)
                candidates.extend((m.start(), m.end(), pid) for m in re.finditer(pattern, text, re.I))
        spans = []
        for start, end, pid in sorted(candidates, key=lambda x: -(x[1]-x[0])):
            if not any(start < b and end > a for a,b,_ in spans): spans.append((start,end,pid))
        positions = sorted((min(a for a,_,p in spans if p==pid),pid) for pid in named if any(p==pid for _,_,p in spans))
        shared_queries = _shared_variant_queries(text, products) if quote_items else {}
        entries = []
        for i, (start, pid) in enumerate(positions):
            part = text[start:positions[i+1][0] if i+1 < len(positions) else len(text)]
            evidence_part = (text[:start] if i == 0 else '') + part
            if (_purchase_question(evidence_part) or _past_purchase(evidence_part)) and not _choice_declaration(evidence_part) and not quote_items: continue
            corrections = positive_corrections(part)
            current = part[corrections[-1].end():] if corrections else evidence_part
            shared = shared_queries.get(pid)
            if shared: current = shared['text']
            current = '，'.join(clause for clause in re.split(r'[，,；;]', current)
                               if not re.search(r'(?:之前|以前|原来).*(?:不选|不要|不穿)|(?:码|色)(?:不选了|不要了|不穿了)', clause))
            colors = list(dict.fromkeys(re.findall(_query_colour_pattern(products) if quote_items else COLORS, current)))
            sizes = list(dict.fromkeys(re.findall(SIZE, current, re.I))) or list(dict.fromkeys(re.findall(SIZE, part, re.I)))
            counts = re.findall(COUNT, current)
            quantity = None
            if len(set(counts)) == 1:
                raw = counts[0]
                quantity = int(raw) if raw.isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[raw]
            entries.append({'product_id': pid, 'colors': colors, 'sizes': sizes, 'quantity': quantity,
                            'source_message_id': source, 'source_text': (shared['source_text'] if shared else part)[:1000],
                            'selection_kind': 'explicit_choice' if explicit and not quote_items else 'quote_request'})
        if entries:
            state.update(selections=entries, source_message_id=source)
            if explicit and not quote_items: state['goal'] = None
    elif not named and explicit and len(state.get('selections', [])) == 1:
        # A color/size/quantity correction can refer to the single selected item.
        item = state['selections'][0]
        colors = list(dict.fromkeys(re.findall(COLORS, text)))
        sizes = list(dict.fromkeys(re.findall(SIZE, text, re.I)))
        counts = re.findall(COUNT, text)
        if colors:
            item['colors'] = colors
            item.pop('color_needs_confirmation', None)
        if sizes: item['sizes'] = sizes
        if len(set(counts)) == 1:
            raw = counts[0]
            item['quantity'] = int(raw) if raw.isdigit() else {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}[raw]
        if colors or sizes or counts:
            item.update(source_message_id=source, source_text=text[:1000], selection_kind='explicit_choice')
            state['source_message_id'] = source
    # Keep an explicitly requested category even when it has no catalog item.
    category = re.search(CATEGORIES, text)
    if category and not factual and re.search(GOAL_ACTION, text):
        state['goal'] = {'category': category.group(), 'text': text[:1000], 'source_message_id': source}
    return state


def request(preferences, text, *, products=(), product_ids=()):
    """Resolve explicit return-to-selection requests, never a vague new topic."""
    mixed = bool(re.search(r'另外|还有|顺便|同时|售后|退货|退款|维修|物流|人工|资格|[；;]', text))
    state = preferences.get('shopping', {})
    personal_ref = re.search(r'(?:我自己|本人)(?:的)?(?:那件|那款|那副|那套)', text)
    selection_ref = re.search(r'最后(?:确认|选定|选择)|更改后的选择|这两(?:种商品|款商品|款)|刚才(?:确认|选定|选择)', text) or personal_ref
    goal_ref = re.search(r'按我的(?:使用场景|选购要求)|回到(?:最初|原先|之前)(?:的)?(?:需求|场景|要求)', text)
    if selection_ref and re.search(r'报价|实付|价格|库存|小计', text):
        unknown_models = (re.findall(r'(?<![A-Za-z0-9_-])[A-Za-z]{1,3}\d+(?:\s*Pro)?(?![A-Za-z0-9_-])', text, re.I)
                          if personal_ref and products and not product_ids else [])
        tokens = ('请','帮我','给我','给','按','把','重新','再','列出','列','一下','最后确认','最后选定','最后选择',
                  '刚才确认','刚才选定','刚才选择','刚才','更改后的选择','那件衣服','那款商品','那件','那款',
                  '这两种商品','这两款商品','这两款','的','各自','当前','实付','小计','报价','价格','库存','和','与','及','选择','报','总价')
        if personal_ref:
            tokens += ('现在', '我自己', '本人', '自己', '实际', '那副', '那套')
            tokens += tuple(str(label) for p in products if p['id'] in product_ids
                            for label in [p['id'], p.get('model'), p.get('name'), *p.get('aliases', [])] if label)
            tokens += tuple(unknown_models)
        residual = re.sub('|'.join(map(re.escape,sorted(tokens,key=len,reverse=True))), '', text)
        complete = not mixed and not re.search(r'[^\s，,。.!！?？、]', residual)
        items = [item for item in state.get('selections', [])
                 if not unknown_models and (not personal_ref or not product_ids or item['product_id'] in product_ids)]
        return {'kind':'selections' if items else 'missing', 'items':items, 'complete':complete,
                'unknown_models': unknown_models}
    if goal_ref and state.get('goal'):
        residual = re.sub(r'按我的使用场景|按我的选购要求|回到(?:最初|原先|之前)(?:的)?(?:需求|场景|要求)|给出|提供|有依据|选择|推荐|请|的|我', '', text)
        return {'kind':'goal', 'goal':state['goal'],
                'complete':not mixed and not re.search(r'[^\s，,。.!！?？、]', residual)}
    return None


def query(item):
    return ' '.join([item['product_id'], *item.get('colors', []), *item.get('sizes', []),
                     str(item['quantity'])+'件' if item.get('quantity') else ''])
