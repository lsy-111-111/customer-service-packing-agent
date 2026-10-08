"""Finite, source-backed state for one conversation, never a customer profile.

Only new customer messages are ingested. Tombstones therefore survive replay,
restart and summary generation; an explicit new statement can replace them.
"""
import json
import re
import logging
from decimal import Decimal
from fastapi import HTTPException
from . import db, shopping_state, request_fragments

log = logging.getLogger(__name__)

KEYS = {'budget_max_cents', 'colors', 'excluded_colors', 'sizes', 'excluded_sizes', 'usage', 'required_features', 'excluded_features', 'excluded_products'}
LIST_KEYS = KEYS - {'budget_max_cents', 'usage'}
PATTERNS = {
    'status': r'进度|什么状态|到哪[一]?步|处理到|处理完成|审核[了好]?吗|审核同意|退[款货]到哪|到账|已同意|待处理|待审核|待补材料|(?:签收|退款|退货|维修|保修|物流|售后|订单)(?:的)?状态|是否签收|签收(?:日期|时间|了[吗没])|售后(?:申请)?记录(?:吗|么)|(?<!调)(?:查|查询|核对|确认|有没有|有无|看)[^，,。；;！？?]{0,100}(?:签收|售后(?:申请)?记录)',
    'price': r'多少钱|价格|报价|单价|售价|现价|差价|价差|贵多少|便宜多少|实付|优惠|到手价',
    'stock': r'有货|库存|缺货|补货|现货|什么时候到货',
    'compare': r'对比|比较|区别|差别|哪个好',
    'recommend': r'推荐|帮我选|挑一|预算|佩戴|不入耳|半入耳|硅胶',
    'after_sale': r'能[不能否]*退|想退|要退|能换|想换|要换|退货|退款|换货|补发|维修|物流|保修|坏了|故障|售后|资格|咨询.{0,12}退',
    'manual': r'人工客服|转人工|找人工|真人客服',
}


def is_new_purchase_statement(text, products=()):
    """An explicit new shopping topic cannot describe an existing order.

    Retain historical purchases and mixed service clauses for the order-aware
    caller to split. This helper only ends attribution for a new choice/goal;
    it never classifies an ordered item's condition or return reason.
    """
    if re.search(r'售后|退货|退款|换货|补发|维修|保修|故障|损坏|摔坏|破损|少发|漏发|'
                 r'到货|收到|签收|实收|商品完好|配件齐全|仅试穿|只试穿', text):
        return False
    if ((shopping_state._past_purchase(text) or re.search(r'(?:买|购买|下单)(?:来)?的', text))
            and not shopping_state._choice_declaration(text)):
        return False
    state = shopping_state.update({}, text, products, 'scope-check', _products)
    return (any(item.get('selection_kind') == 'explicit_choice'
                for item in state.get('selections', []))
            or bool(state.get('goal')))


def init_schema(c):
    schema = '''
    CREATE TABLE IF NOT EXISTS conversation_state(conversation_id TEXT PRIMARY KEY,version INTEGER NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS service_intents(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,source_message_id TEXT NOT NULL,status TEXT NOT NULL,resolution TEXT NOT NULL DEFAULT 'unknown',data TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS intents_conversation ON service_intents(conversation_id,status);
    '''
    for statement in schema.split(';'):
        if statement.strip(): c.execute(statement)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _load(c, cid):
    row = c.execute('SELECT * FROM conversation_state WHERE conversation_id=?', (cid,)).fetchone()
    if row:
        data = json.loads(row['data'])
        return {**data, 'version': row['version'], 'updated_at': row['updated_at']}
    return {'version': 0, 'items': [], 'processed_rowid': 0, 'scope': '会话', 'updated_at': None}


def _save(c, cid, state):
    state['updated_at'] = db.now()
    c.execute('INSERT INTO conversation_state VALUES (?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET version=excluded.version,data=excluded.data,updated_at=excluded.updated_at',
              (cid, state['version'], _dump(state), state['updated_at']))


def public(state):
    return {**{k: state[k] for k in ('version', 'items', 'scope', 'updated_at')},
            'shopping': state.get('shopping', {'selections': [], 'goal': None}),
            'conditional_requests': state.get('conditional_requests', [])}


def _identity(item):
    return item['key'], item['scope'], item.get('product_id')


def _set(state, key, value, scope, source, product_id=None, source_text=None, overrides_global=False):
    if value == []: value = None
    state['items'] = [i for i in state['items'] if _identity(i) != (key, scope, product_id)]
    item = {'key': key, 'value': value, 'scope': scope, 'source_message_id': source,
            'active': value is not None, 'version': state['version'] + 1}
    if product_id: item['product_id'] = product_id
    if source_text: item['source_text'] = source_text[:1000]
    if overrides_global: item['overrides_global'] = True
    state['items'].append(item)


def choice_items(preferences):
    """Current explicitly chosen variants, separate from personal preferences.

    A price inquiry's specifications are not a choice. These projected records
    never enter persisted preference items or revive UI-deleted requirements.
    """
    items = []
    for selection in preferences.get('shopping', {}).get('selections', []):
        if selection.get('selection_kind') != 'explicit_choice': continue
        for key in ('colors', 'sizes'):
            if not selection.get(key): continue
            normalize = shopping_state.color_key if key == 'colors' else str.upper
            if any(i.get('active') and i['key'] == key and i.get('product_id') == selection['product_id']
                   and {normalize(v) for v in i['value']} == {normalize(v) for v in selection[key]}
                   for i in preferences.get('items', [])): continue
            items.append({'key':key, 'value':selection[key], 'scope':'会话',
                          'product_id':selection['product_id'], 'active':True,
                          'source_message_id':selection.get('source_message_id'),
                          'source_text':selection.get('source_text'), 'overrides_global':True,
                          'basis':'current_explicit_choice'})
    return items


def effective_items(preferences, product):
    """Project authoritative state onto one catalogue product, without replay.

    Missing product_id preserves the legacy category/session meaning. Explicit
    targeted color/size corrections may override that key's general requirement;
    exclusions and unrelated features remain in force. Source validation belongs
    to the caller when state did not come from the conversation store.
    """
    items = [i for i in [*preferences.get('items', []), *choice_items(preferences)] if i.get('active') is True
             and i.get('value') is not None and i.get('scope') in ('会话', product.get('category'))
             and i.get('product_id') in (None, product.get('id'))]
    result = []
    local = [i for i in items if i.get('product_id') and i.get('overrides_global')]
    for item in items:
        if item.get('product_id'):
            result.append(item)
            continue
        key = item['key']
        if key in ('colors', 'sizes') and any(i['key'] == key for i in local): continue
        opposite = {'colors':'excluded_colors', 'excluded_colors':'colors',
                    'sizes':'excluded_sizes', 'excluded_sizes':'sizes'}.get(key)
        changed = [i for i in local if i['key'] == opposite]
        if changed and isinstance(item['value'], list):
            normalize = shopping_state.color_key if 'color' in key else str.upper
            removed = {normalize(v) for i in changed for v in i['value']}
            remaining = [v for v in item['value'] if normalize(v) not in removed]
            if remaining: result.append({**item, 'value':remaining})
        else: result.append(item)
    return result



def _product_aliases(products):
    aliases = {}
    for product in products:
        for alias in [product['id'], product.get('model', ''), product.get('name', ''), *product.get('aliases', [])]:
            if alias: aliases.setdefault(str(alias).lower(), set()).add(product['id'])
    return {alias: next(iter(ids)) for alias, ids in aliases.items() if len(ids) == 1}


def _products(text, products):
    # Prefer long names so E1 Pro does not also select E1.
    remaining = text.lower()
    found = []
    aliases = _product_aliases(products).items()
    for alias, pid in sorted(aliases, key=lambda x: len(x[0]), reverse=True):
        pattern = r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])' if alias.isascii() else re.escape(alias)
        if re.search(pattern, remaining):
            if pid not in found: found.append(pid)
            remaining = re.sub(pattern, ' ', remaining)
    return found


def _scope(text, products, current, selected=None):
    matches = _products(text, products)
    if matches:
        return next((p.get('category', '会话') for p in products if p['id'] == matches[0]), current)
    for regex, category in [(r'耳机|数码|佩戴|半入耳|硅胶', '数码'), (r'水壶|风扇|家电', '家电'),
                            (r'衣服|服饰|衬衫|短袖|长袖|T恤|外套|尺码', '服饰'), (r'杯子|水杯|家居|毛巾', '家居')]:
        if re.search(regex, text, re.I): return category
    if current != '会话': return current
    return next((p.get('category', current) for p in products if p['id'] == selected), current)


_UNCERTAIN = r'如果|假如|假设|要是|若(?:我|自己|本人|将来|以后|未来)|比如|例如|不确定|没想好|还没决定|你说|您说|别人|朋友|比较.{0,8}预算|对比.{0,8}预算'
_DIRECTIVE = re.compile(r'不再排除|可以接受|不强求|不要求|不需要|不用|不要|不想要|不想|不选|不接受|不能接受|排除|拒绝|不喜欢|不考虑|无|不|改成|改为|换成|换为|改|换|选择|只买|仅买|只选|只要|只考虑|仅考虑|只接受|需要|要求|必须|喜欢|想要|要|选')
_FEATURES = {'半入耳式': '半入耳', '半入耳': '半入耳', '入耳式': '入耳式', '入耳': '入耳式',
             '硅胶耳塞': '硅胶耳塞', '硅胶': '硅胶耳塞', '主动降噪': '主动降噪',
             '通话降噪': '通话降噪', '双设备': '双设备', '无线充电': '无线充电',
             '全棉': '棉', '纯棉': '棉', '棉': '棉', '保温': '保温'}
_COLORS = (*shopping_state.COLOR_WORDS, '沙色')


def _asserted_clauses(text):
    # Do not turn hypothetical/comparison/reported statements into a profile.
    # A following independent correction can still establish a new preference.
    result, uncertain = [], False
    for clause in re.split(r'([，,。；;！？?\n])', text):
        if re.fullmatch(r'[。；;！？?\n]', clause):
            uncertain = False
            continue
        if not clause.strip() or clause in ('，', ','): continue
        denied_task = re.match(r'^\s*(?:我)?(?:'
            r'(?:不是|并非)(?:让|要|想让|要让)(?:你|您)?|'
            r'(?:不用|不需要|不要)(?:再)?(?:帮我|给我))'
            r'(?:再)?(?:推荐|帮我选|挑一)', clause)
        if denied_task:
            # Declining the assistant's task is not rejecting its mentioned
            # colour or declaring its sample budget. A later affirmative
            # request keeps its own scope even without a comma.
            following = re.search(r'而是|但是|不过|但|另外|同时|并且', clause[denied_task.end():])
            if following is None: continue
            clause = clause[denied_task.end() + following.end():]
        if re.search(r'^\s*(?:但|不过)?(?:我)?(?:现在决定|现在确定|实际预算|明确要求)', clause): uncertain = False
        if re.search(r'^\s*(?:但|不过)?(?:我自己|我本人|本人)(?:现在)?(?:改|决定|确定|选择|要|只穿)', clause): uncertain = False
        if re.search(_UNCERTAIN, clause) or re.search(r'^\s*(?:FAQ|资料|说明|页面|商品详情|规则).{0,10}(?:说|写|提到|显示|标注|推荐|举例)',clause,re.I): uncertain = True
        if uncertain: continue
        if re.search(r'^\s*(?:我)?(?:之前|以前|原来|此前|刚才).{0,12}(?:预算|喜欢|选择|要)', clause) and not re.search(r'改到|改成|改为|现在', clause): continue
        result.append(clause)
    return result


def _mention_actions(clause, aliases, implicit=False):
    pattern = '|'.join((r'(?<![a-z0-9])'+re.escape(a)+r'(?![a-z0-9])') if a.isascii() else re.escape(a) for a in sorted(aliases, key=len, reverse=True))
    last_end, inherited, actions = 0, None, {}
    for match in re.finditer(pattern, clause, re.I):
        before, after = clause[last_end:match.start()], clause[match.end():]
        # Instructions about what the assistant may infer/explain are not
        # purchase constraints, even when they mention a known feature/color.
        local_before = re.split(r'但是|不过|但|另外|同时|并且', before)[-1]
        speech = r'(?:不要|不用|不需要|不必|别)(?:再|去|随意|擅自|凭空|继续|直接)?(?:推测|推断|猜测|猜|编造|介绍|解释|回答|说明|描述|宣称|断言|保证)'
        if re.search(speech, local_before) or re.match(r'(?:的)?(?:性能|功能|连接)?' + speech, after):
            inherited, last_end = None, match.end()
            continue
        # A restriction on the assistant's recommendation/description is not
        # a rejection of the colour or feature named in that description.
        # Keep a later independent "但我只要…" directive eligible.
        purpose = re.split(r'但是|不过|但|另外|同时|并且', after)[0]
        if (re.search(r'(?:不|别|不要|不用|不必|不需要)(?:再)?(?:作为|当作|视为|用作)', local_before)
                and re.search(r'推荐|建议|说明|介绍|描述|结论', purpose)):
            inherited, last_end = None, match.end()
            continue
        # "是否需要降噪" asks about a requirement; deleting 是否 would
        # manufacture a personal 必须/需要 constraint. This also keeps both
        # branches of a requested comparison available without changing an
        # earlier explicitly stated preference.
        if re.search(r'(?:是否|需不需要|要不要|用不用)(?:(?:还|再)?(?:需要|要求|必须|要|选|选择|接受|排除))?\s*$', local_before):
            inherited, last_end = None, match.end()
            continue
        # Interrogatives are descriptions/questions, never a bare 不 directive.
        directive_text = re.sub(r'是不是|是否|能不能|可不可以|有没有', '', before)
        directives = list(_DIRECTIVE.finditer(directive_text))
        action = inherited if re.fullmatch(r'[、和及与或\s]*', before) else None
        if directives:
            word = directives[-1].group()
            action = 'allow' if word in ('不再排除', '可以接受') else 'remove' if word in ('不强求', '不要求', '不需要') else 'exclude' if word.startswith('不') or word in ('不用', '排除', '拒绝', '无') else 'require'
        if re.match(r'(?:可以接受|也可以|也行)', after): action = 'allow'
        negative = re.match(r'\s*(?:(?:明确|坚决|这次|现在|还是)\s*)?(?:不要(?:了)?|不考虑(?:了)?|不选(?:了)?|不买(?:了)?|不喜欢|不接受)', after)
        if negative and not re.match(r'\s*(?:吗|么|是否|[？?])', after[negative.end():]): action = 'exclude'
        if re.match(r'(?:不要求了|不需要了)', after): action = 'remove'
        if action is None and implicit: action = 'require'
        if action is not None: actions[aliases[match.group().lower()]] = action
        inherited, last_end = action, match.end()
    return actions


def _budget_withdrawal(clause):
    """Recognize an asserted removal, with its subject before or after it."""
    if re.search(r'吗|么|是否|能否|可不可以|能不能', clause):
        return False
    action = r'取消|撤销|撤回|去掉|解除'
    pattern = (r'预算(?:不限|不限制|没有限制|无上限)|不限制预算|'
               r'(?:不设|不设置)(?:本次|这次|当前|任何)?(?:的)?预算(?:上限|限制)?|'
               r'预算(?:不设|不设置)(?:上限|限制)|'
               r'(?:' + action + r')(?:这次|当前|本次|之前|原来|以前|旧)?(?:的)?预算(?:上限|限制)?|'
               r'预算(?:上限|限制)?(?:已|已经)?(?:' + action + r'|不再生效|不生效|作废)')
    for match in re.finditer(pattern, clause):
        before, after = clause[:match.start()], clause[match.end():]
        if re.search(r'(?:不要|不用|不必|别|不能|不想|不是|没有|并未|未|没)(?:再|去|把|说要)?\s*$', before):
            continue
        if re.match(r'不了|不掉|失败|不成功', after):
            continue
        return True
    return False


def changes_from_text(text, products):
    changes, colors, features, product_actions = {}, {}, {}, {}
    clauses = _asserted_clauses(text)
    budget_subject = False
    # "Only X is insufficient" qualifies an affirmative required capability;
    # it does not prohibit products that also have X. Preserve independent
    # explicit exclusions, and do not infer a missing positive requirement.
    feature_aliases = {k.lower(): v for k, v in _FEATURES.items()}
    required = {feature for part in clauses
                for feature, action in _mention_actions(part, feature_aliases).items()
                if action == 'require'}
    only_pattern = (r'^\s*(?:只有|仅有|只具备|仅具备|只支持|仅支持)\s*('
                    + '|'.join(re.escape(k) for k in sorted(feature_aliases, key=len, reverse=True))
                    + r')(?:的)?(?:款|型号|商品)?(?:不考虑|不选|不要|不接受)(?:了)?\s*$')
    # Explicit corrections take their new amount, never the number after 从.
    budget_pattern = r'(?:预算|不超过|最多|上限)(?:是|为|在|改为|改成|改到|调整到|提高到|降低到|不超过|最多|只有|就)?\s*[¥￥]?\s*(\d{1,7}(?:\.\d{1,2})?)(?![\d.])\s*(?:元|块)?'
    for clause in clauses:
        recommendation = request_fragments.recommendation_clause(clause)
        # A customer can state a shopping ceiling without saying "预算" or
        # asking for a recommendation. Do not borrow freight, order payments
        # or a product's advertised price as the customer's spending limit.
        ceiling = re.search(r'(?<![\d.])(\d{1,7}(?:\.\d{1,2})?)(?![\d.])\s*(?:元|块)?\s*(?:以内|以下|内)', clause)
        shopping_ceiling = bool(ceiling and (recommendation or
            ((re.search(shopping_state.CATEGORIES, clause) or _products(clause, products))
             and not re.search(r'运费|邮费|维修费|退款|订单|原实付|历史|售价|价格|报价|实付|库存|容量|重量|为什么|为啥|如何|怎么|解释|说明|介绍|参数', clause))))
        if not shopping_ceiling: ceiling = None
        other_money = bool(not re.search(r'预算', clause) and
                           re.search(r'运费|邮费|维修费|退款|订单|原实付|历史|售价|价格|报价|实付', clause))
        matches = list(re.finditer(budget_pattern, clause))
        reverse_ceiling = re.search(
            r'(?<![\d.])(\d{1,7}(?:\.\d{1,2})?)(?![\d.])\s*(?:元|块)\s*'
            r'(?:就是|是|为|作为)(?:我(?:的)?|本次|这次)?(?:的)?(?:严格|最高|硬性)?'
            r'(?:预算)?(?:上限|最高限额)', clause)
        if reverse_ceiling and (re.search(r'吗|么|是否|能否|[？?]|不要记录|不要记|别记|不能记', clause)
                                or re.search(r'最初|原先|之前|以前|原来|此前', clause[:reverse_ceiling.start()])):
            reverse_ceiling = None
        correction = re.search(r'预算[^。；\n]{0,30}?(?:改到|改成|改为|调整到|提高到|降低到|改|现在(?:是|为))\s*[¥￥]?\s*(\d{1,7}(?:\.\d{1,2})?)(?![\d.])', clause)
        if not correction and budget_subject and not re.search(r'吗|么|是否|能否', clause):
            correction = re.search(r'^\s*(?:现在|这次)(?:明确)?(?:改到|改成|改为|调整到|提高到|降低到|改)\s*[¥￥]?\s*(\d{1,7}(?:\.\d{1,2})?)(?![\d.])', clause)
        budget_subject = budget_subject or bool(re.search(r'预算', clause)) or shopping_ceiling or bool(reverse_ceiling)
        interval = re.search(r'预算\s*\d+(?:\.\d{1,2})?\s*(?:元)?(?:到|至|[-~～])\s*(\d{1,7}(?:\.\d{1,2})?)(?![\d.])', clause)
        amount = correction[1] if correction else interval[1] if interval else reverse_ceiling[1] if reverse_ceiling else matches[-1][1] if matches else None
        if amount is None and ceiling: amount = ceiling[1]
        if other_money: amount = None
        # Historical ceilings establish only the subject of a following
        # explicit current correction. They cannot revive a withdrawn budget.
        amount_match = correction or interval or reverse_ceiling or (matches[-1] if matches else ceiling)
        amount_prefix = clause[:amount_match.start(1)] if amount_match else ''
        if not correction and re.search(r'最初|原先|之前|以前|原来|此前', amount_prefix): amount = None
        if not correction and re.search(r'(?:不是|不要按|别按|别把|不能按|不要记|之前的|以前的|旧的).{0,6}预算|预算.{0,16}(?:不对|错误|说错)', clause): amount = None
        if amount is not None and not re.search(r'至少|以上|起步|预算.{0,16}(?:和|还是|与|或者).{0,8}\d|预算.{0,16}(?:是否|可以吗|行吗|怎么样)', clause):
            cents = int(Decimal(amount) * 100)
            if cents <= 100000000: changes['budget_max_cents'] = cents
        if _budget_withdrawal(clause): changes['budget_max_cents'] = None
        bare = not re.search(r'什么|怎么|区别|差别|支持|有货|库存|多少钱|价格|报价|吗|是否|能否|有没有|是不是|需不需要|要不要|用不用|比较|对比|相比|还是|或者', clause)
        # A bare color/feature list is an explicit constraint; descriptions of
        # a product ("E1是入耳式") are not the customer's requirement.
        implicit = bare and (recommendation or bool(re.fullmatch(r'\s*(?:' + '|'.join(map(re.escape,_COLORS)) + r'|半入耳(?:式)?|入耳式|主动降噪|通话降噪|硅胶(?:耳塞)?|双设备|无线充电|全棉|纯棉|棉|保温|[、和及与或\s])+\s*', clause)) or ('budget_max_cents' in changes and not _products(clause, products)) or bool(re.match(r'^\s*(?:半入耳(?:式)?|白色|黑色)', clause) and _DIRECTIVE.search(clause)))
        colors.update(_mention_actions(clause, {c: c for c in _COLORS}, implicit))
        only = re.fullmatch(only_pattern, clause)
        if not (only and required - {feature_aliases[only[1].lower()]}):
            features.update(_mention_actions(clause, feature_aliases, implicit))
        if re.search(r'颜色(?:不限|无所谓|不限制)|取消颜色', clause):
            changes['colors'] = changes['excluded_colors'] = None
            colors = {}
        if re.search(r'取消(?:所有|全部)?(?:功能|性能)要求', clause): changes['required_features'] = None
        aliases = _product_aliases(products)
        product_actions.update(_mention_actions(clause, aliases) if aliases else {})
        usage = re.search(r'(?:用于|用来|主要用[于来]?|用途[是为]?)([^，。；？\n]{1,36})', clause)
        if usage and not re.search(r'什么|怎么|吗|是否', usage[1]) and not re.search(r'(?:不是|并非|不|别|不要).{0,4}(?:用于|用来)', clause): changes['usage'] = usage[1]
        elif bare and re.search(r'通勤|运动|办公|送礼|旅行', clause) and not re.search(r'不|别', clause):
            changes['usage'] = '、'.join(dict.fromkeys(re.findall(r'通勤|运动|办公|送礼|旅行', clause)))
    for actions, positive, negative in ((colors, 'colors', 'excluded_colors'), (features, 'required_features', 'excluded_features')):
        for action, key in [('require', positive), ('exclude', negative), ('remove', '_remove_' + positive), ('allow', '_remove_' + negative)]:
            values = [v for v, a in actions.items() if a == action]
            if values: changes[key] = values
    for action, key in [('exclude', 'excluded_products'), ('require', '_remove_excluded_products'), ('allow', '_remove_excluded_products')]:
        values = [v for v, a in product_actions.items() if a == action]
        if values: changes[key] = list(dict.fromkeys([*changes.get(key, []), *values]))
    return changes


def _apply_changes(state, changes, scope, source, *, merge, product_id=None, source_text=None, overrides_global=False):
    for key, value in changes.items():
        removing = key.startswith('_remove_')
        field = key[len('_remove_'):] if removing else key
        if field not in KEYS: continue
        old = next((i for i in state['items'] if _identity(i) == (field, scope, product_id) and i['active']), None)
        if removing:
            value = [v for v in (old['value'] if old else []) if v not in value] or None
        elif merge and field in ('required_features', 'excluded_features', 'excluded_products', 'excluded_colors', 'excluded_sizes') and old and value:
            value = list(dict.fromkeys([*old['value'], *value]))
        _set(state, field, value, scope, source, product_id, source_text, overrides_global)
    for key, opposite in [('required_features', 'excluded_features'), ('excluded_features', 'required_features'), ('colors', 'excluded_colors'), ('excluded_colors', 'colors'), ('sizes', 'excluded_sizes'), ('excluded_sizes', 'sizes')]:
        if changes.get(key):
            prior = next((i for i in state['items'] if _identity(i) == (opposite, scope, product_id) and i['active']), None)
            if prior:
                excluded = {shopping_state.color_key(v) for v in changes[key]} if key in ('colors','excluded_colors') else set(changes[key])
                _set(state, opposite, [v for v in prior['value'] if (shopping_state.color_key(v) if key in ('colors','excluded_colors') else v) not in excluded] or None, scope, source, product_id, source_text, overrides_global or prior.get('overrides_global', False))


_SPEC_KEYS = {'colors', 'excluded_colors', '_remove_colors', '_remove_excluded_colors', 'sizes', 'excluded_sizes'}


def _product_spans(text, products):
    found = []
    for alias, pid in sorted(_product_aliases(products).items(), key=lambda pair: -len(pair[0])):
        pattern = r'(?<![a-z0-9])' + re.escape(alias) + r'(?![a-z0-9])' if alias.isascii() else re.escape(alias)
        for match in re.finditer(pattern, text, re.I):
            if not any(match.start() < end and match.end() > start for start, end, _ in found):
                found.append((match.start(), match.end(), pid))
    return sorted(found)


def _spec_updates(text, products, default_target=None):
    """Keep customer-stated specifications attached to their actual model.

    General category requests keep a null target. Queries and hypothetical or
    third-party statements do not establish personal requirements. A requested
    alternative can constrain its displayed SKU without becoming a purchase.
    """
    target = default_target
    asserted = set(_asserted_clauses(text))
    for part in re.split(r'([，,。；;！？?\n])', text):
        if re.fullmatch(r'[。；;！？?\n]', part):
            target = None
            continue
        if not part.strip() or part in ('，', ','): continue
        if part not in asserted: continue
        spans = _product_spans(part, products)
        # Within a sentence an explicit product remains the referent, except
        # when the customer explicitly returns to the whole category.
        general = bool(re.search(r'全部|所有|都(?:要|改|换|不)|耳机|数码|家电|服饰|家居', part)) and not spans
        if general: target = None
        chunks = []
        if spans:
            for index, (start, _, pid) in enumerate(spans):
                chunks.append((pid, part[0 if index == 0 else start:spans[index+1][0] if index+1 < len(spans) else len(part)]))
        else: chunks = [(target, part)]
        for pid, chunk in chunks:
            target = pid
            changes = {k:v for k,v in changes_from_text(chunk, products).items() if k in _SPEC_KEYS}
            # "Please give model/color in stock" requests this exact option;
            # merely asking whether another color exists is not a preference.
            if pid and re.search(r'(?:请|帮我|给我|另)?给(?:我)?', chunk) and not re.search(r'别给|不给|不要给|是否|能否|吗', chunk):
                requested = changes_from_text(re.sub(r'给(?:我)?', '要', chunk), products)
                changes.update({k:v for k,v in requested.items() if k in _SPEC_KEYS})
            personal = re.search(r'(?:我|本人|自己)(?:现在|这次|还是)?(?:只|仅|必须|只能|现在改|改)?穿\s*(' + shopping_state.SIZE + r')', chunk, re.I)
            if personal:
                changes['sizes'] = list(dict.fromkeys(v.upper() for v in re.findall(shopping_state.SIZE, re.split(r'但|不过|后面|随后|帮|朋友', chunk[personal.start():])[0], re.I)))
            if re.search(r'(?:我|本人|自己)(?:现在|这次)?不穿', chunk):
                changes['excluded_sizes'] = list(dict.fromkeys(v.upper() for v in re.findall(shopping_state.SIZE, chunk, re.I)))
            if re.search(r'(?:取消|不再限制|不限)(?:个人)?尺码(?:要求|条件)?|尺码不限', chunk):
                changes['sizes'] = changes['excluded_sizes'] = None
            if changes:
                yield pid, changes, chunk, general


def _apply_preference_segment(state, segment, products, scope, source, order_target=None):
    changes = changes_from_text(segment, products)
    general = {key:value for key,value in changes.items() if key not in _SPEC_KEYS}
    _apply_changes(state, general, scope, source, merge=True, source_text=segment)
    changed = bool(general)
    selections = state.get('shopping', {}).get('selections', [])
    default_target = order_target or (selections[0]['product_id'] if order_target is None and len(selections) == 1 and re.search(r'改成|改为|换成|换为|现在改|现在换|改穿', segment) else None)
    for pid, specs, chunk, reset_general in _spec_updates(segment, products, default_target):
        if order_target and not pid:
            pid, reset_general = order_target, False
        # An unknown/ambiguous order cannot create an unscoped preference or
        # borrow a prior shopping choice. An explicit model remains its source.
        if order_target is False and not pid:
            continue
        if pid and re.search(r'改成|改为|换成|换为|现在改|现在换|改穿', chunk):
            sizes = [size.upper() for size in re.findall(shopping_state.SIZE, chunk, re.I)]
            if sizes and any(i.get('active') and i['key'] == 'sizes' and i.get('product_id') == pid for i in state['items']):
                specs['sizes'] = list(dict.fromkeys(sizes))
        if reset_general:
            # Explicit whole-category corrections supersede older local values,
            # leaving tombstones so history cannot revive them.
            for item in list(state['items']):
                if item.get('product_id') and item['scope'] == scope and item['key'] in specs:
                    _set(state, item['key'], None, scope, source, item['product_id'], chunk)
        override = bool(pid and re.search(r'例外|除外|改|换|(?:我)?(?:只|就)?要|选择|我选|给|只穿|改穿', chunk))
        _apply_changes(state, specs, scope, source, merge=True, product_id=pid,
                       source_text=chunk, overrides_global=override)
        changed = True
    return changed


def _preference_focus_text(text):
    """A deferred order action cannot replace the active preference topic."""
    deferred = re.search(r'(?:先|暂(?:时|且)?)?(?:不用|不要|不需要|无需|不必|不)'
                         r'(?:再|继续)?(?:处理|办理|申请|提交|整理)', text)
    if (not deferred or not re.search(r'订单|那(?:笔|单)|这(?:笔|单)|售后|申请|草稿', text[:deferred.start()])
            or re.search(r'如果|假如|假设|是否|怎么|如何|为什么|吗|么|[？?“”「」"]', text)
            or re.search(r'别说|不要说|不是|并非|不能说|不代表', text[:deferred.start()])):
        return text
    remainder = text[deferred.end():]
    continuation = re.search(r'但是|不过|而是|另外|同时|并且|但', remainder)
    return remainder[continuation.end():] if continuation else ''


def _preference_segments(text, products, current, selected):
    pieces = re.split(r'([，,。；;\n])', '，'.join(_asserted_clauses(text)))
    segments, buffer, scope = [], '', current
    for piece in pieces:
        candidate = _scope(_preference_focus_text(piece), products, scope, selected)
        if candidate != scope and buffer.strip():
            segments.append((scope, buffer)); buffer = ''
        scope = candidate; buffer += piece
    if buffer.strip(): segments.append((scope, buffer))
    return segments or [(current, text)]


def _order_preference_blocks(text, products, orders, selected_order):
    """Keep implicit exchange specifications on their owned order's product.

    None means ordinary shopping; False means an explicit order could not be
    uniquely resolved. Customer text is never rewritten to invent a model.
    A separate purchase/requirement starts a fresh ordinary shopping block.
    """
    owned = {o['id']: o.get('product_id') for o in orders}
    target = owned.get(selected_order) if re.search(r'换货|想换|要换|换成|换为|换同款', text) else None
    if not _order_mentions(text, orders) and not _has_unknown_order(text, orders) and target is None:
        return [(text, None)]
    blocks, buffer = [], ''
    for piece in re.split(r'([，,。；;！？?\n]|另外|另问|顺便|同时)', text):
        if not piece: continue
        previous = target
        focus = _preference_focus_text(piece)
        mentions = list(dict.fromkeys(m[2] for m in _order_mentions(focus, orders)))
        if mentions or _has_unknown_order(focus, orders):
            target = owned.get(mentions[0]) if len(mentions) == 1 and not _has_unknown_order(focus, orders) else False
            target = target or False
        elif re.search(r'推荐|选购|(?:想|要|再|另|新)买|(?:我|本人|自己)(?:一直|通常|平时|都|更)?喜欢|(?:耳机|数码|家电|服饰|家居|衣服).{0,4}都(?:要|选|穿)', focus):
            target = None
        else:
            named = _products(focus, products)
            if named and target not in named:
                target = None
        if target != previous and buffer.strip():
            blocks.append((buffer, previous)); buffer = ''
        buffer += piece
    if buffer.strip(): blocks.append((buffer, target))
    return blocks or [(text, target)]


def _withdraw_intents(c, cid, text, message_id, products, orders):
    text = '，'.join(clause for clause in _asserted_clauses(text)
                    if not re.search(r'(?:别|不要|不用|不需要|不想)(?:再)?(?:取消|撤回)|怎么|如何|能不能|是否|吗', clause))
    rows = c.execute("SELECT * FROM service_intents WHERE conversation_id=? AND status IN ('pending','needs_info') ORDER BY rowid", (cid,)).fetchall()
    all_requested = bool(re.search(r'(?:取消|撤回|不用处理|不需要处理)(?:全部|所有)(?:的)?(?:问题|咨询|请求|诉求)', text))
    latest_requested = bool(re.search(r'(?:取消|撤回|不用处理|不需要处理)(?:本次|刚才)(?:的)?(?:问题|咨询|请求|诉求)', text))
    kinds = []
    for kind, noun in [('price', r'价格|报价|差价'), ('stock', r'库存|补货|有货'), ('compare', r'比较|对比'), ('after_sale', r'退货|退款|换货|补发|售后|维修|物流'), ('manual', r'转人工|人工接管')]:
        if re.search(r'(?:取消|撤回|不用|不要|不需要)(?:再)?(?:查|问|处理|办)?(?:'+noun+r')|(?:'+noun+r').{0,5}(?:不用查了|不问了|不办了|取消|撤回)', text): kinds.append(kind)
    if re.search(r'不退了|不换了', text): kinds.append('after_sale')
    if not all_requested and not latest_requested and not kinds: return False
    pids = _products(text, products)
    oids = [order['id'] for order in orders if order['id'].lower() in text.lower()]
    withdrawal_types = request_types(re.sub(r'不退了', '退货', re.sub(r'不换了', '换货', text)), include_negated=True)
    candidates = []
    for row in rows:
        data = json.loads(row['data'])
        if all_requested or latest_requested or data.get('kind') in kinds:
            if not all_requested and not latest_requested and data.get('kind') == 'after_sale' and withdrawal_types and _intent_request_type(data) not in withdrawal_types: continue
            if pids and not set(pids).intersection(data.get('product_ids', [])): continue
            if oids and not set(oids).intersection(data.get('order_ids', [])): continue
            candidates.append((row, data))
    if latest_requested and candidates:
        latest = candidates[-1][1].get('latest_message_id')
        candidates = [(row, data) for row, data in candidates if data.get('latest_message_id') == latest]
    # "不退了" with several unresolved orders is ambiguous; never cancel all.
    if not all_requested and not latest_requested and not pids and not oids and len(candidates) > 1:
        return True
    for row, data in candidates:
        data['withdrawn_source_message_id'] = message_id
        c.execute("UPDATE service_intents SET status='withdrawn',resolution='withdrawn',data=?,updated_at=? WHERE id=?", (_dump(data), db.now(), row['id']))
    return True


def _withdrawal_clause(text):
    return bool(re.search(r'(?:取消|撤回|不用|不要|不需要).{0,10}(?:请求|问题|咨询|诉求|价格|报价|差价|库存|补货|有货|比较|对比|退货|退款|换货|补发|售后|维修|物流|转人工|人工接管)|(?:价格|报价|差价|库存|补货|有货|比较|对比|退货|退款|换货|补发|售后|维修|物流).{0,5}(?:不用查了|不问了|不办了|取消|撤回)|不退了|不换了', text))


_REQUEST_TYPES = {
    'refund_only': r'不退货.{0,5}(?:只|仅)?退款|(?:仅|只)?退款.{0,5}不退货|仅退款|只退款|只退钱|仅退钱',
    'return_refund': r'退货(?:退款)?|退款|退换|(?:能否|能不能|可以|可否|想|要|直接|怎么|如何)?退(?!换|修|款|回|出|一步)',
    'exchange': r'换货|退换|(?:能否|能不能|可以|可否|能|想|要|希望|需要|直接|怎么|如何|改|或|或者|还是)(?:免费|自费|付费|重新|再次|直接|再)?换|换成',
    'reship': r'补发|补寄|重新寄|补线|补(?:一|两|三|[0-9]+)(?:件|副|条|根|个)',
    'repair': r'维修|保修|修理|返修|(?:能否|能不能|可以|可否|怎么|如何|还能)(?:再|继续)?修',
    'logistics': r'(?:调查|核查|核实|查询|跟进|催促|投诉|处理).{0,6}(?:物流|快递|包裹|配送)|(?:物流|快递|包裹|配送).{0,6}(?:调查|核查|查询|跟进|催促|投诉|怎么处理)|物流|催发货|催收货|催件|签收调查|虚假签收|(?:快递|包裹|配送).{0,8}(?:未收到|没有收到|没收到|没到|未到|丢失|遗失|破损|损坏|延误|异常)|(?:未收到|没有收到|没收到|收不到).{0,6}(?:快递|包裹)',
}


def _explicit_logistics_action(text):
    """A delivery circumstance is not a second request alongside a remedy."""
    return bool(re.search(r'物流争议|签收调查|催发货|催收货|催件|(?:调查|核查|核实|查询|跟进|催促|投诉|处理|问|查).{0,6}(?:物流|快递|包裹|配送)|(?:物流|快递|包裹|配送).{0,6}(?:调查|核查|查询|跟进|催促|投诉|怎么处理|如何处理)', text))


def _repair_history_only(text, match):
    """A past/unknown repair circumstance does not request a new repair."""
    before = re.split(r'[，,。；;！？?\n]|不过|但是', text[:match.start()])[-1]
    after = re.split(r'[，,。；;！？?\n]|不过|但是', text[match.end():])[0]
    # Attach an explicit present request to this mention, not to a different
    # remedy elsewhere in the sentence. Normal warranty/process queries remain.
    request_before = r'(?:现在|这次|还|再)?(?:想|要|希望|需要|申请|办理|咨询|考虑|能|能否|可以|能不能|怎么|如何|还能)(?:申请|办理|再次|再)?$'
    request_after = r'^(?:怎么|如何|流程|方式|渠道|申请|办理|可以吗|行吗|能否|是否|吗|么)'
    if re.search(request_before, before) or re.search(request_after, after): return False
    return bool(re.match(r'^(?:过|了|经历|历史|记录|痕迹|次数|情况|后(?:仍|还是|依然))', after)
                or re.search(r'(?:之前|以前|曾经|上次|此前|已经|已)(?:有过|有|做过|做|曾|经过)?$', before))


def request_types(text, *, include_negated=False):
    """Finite request labels, not authorization to create or execute a case."""
    found = []
    remaining = text
    for kind, pattern in _REQUEST_TYPES.items():
        matches = []
        for match in re.finditer(pattern, remaining):
            if kind == 'repair' and _repair_history_only(remaining, match): continue
            before = remaining[max(0, match.start()-10):match.start()]
            after = remaining[match.end():match.end()+8]
            if kind == 'logistics' and not include_negated:
                if (re.search(r'(?:无需|不用|不必|不需要|未要求|不要|不是|并非|没说|没有说|未说|(?:并)?没有|没|未)(?:再|发生|出现)?$', before)
                        or re.search(r'(?:无需|不用|不必|不需要|不要)(?:再|去)?(?:调查|核查|查询|处理|跟进|催)', match.group() + after)):
                    continue
            if not include_negated and (re.search(r'(?:不要|不想|不需要|不是(?:要|想)?|并非(?:要|想)?|不办理|不申请|取消|撤回)(?:再|办理|申请)?$', before) or before.endswith('不')):
                continue
            if not include_negated and re.match(r'(?:不用了|不办了|取消|撤回)', after):
                continue
            matches.append(match)
        if matches:
            found.append(kind)
            # "仅退款、不退货" is one request, not an extra return request.
            if kind == 'refund_only':
                for match in reversed(matches):
                    remaining = remaining[:match.start()] + ' ' * (match.end()-match.start()) + remaining[match.end():]
    return found


def _order_mentions(text, orders):
    matches = []
    for order in orders:
        identity = order['id']
        for match in re.finditer(r'(?<![a-z0-9_-])'+re.escape(identity)+r'(?![a-z0-9_-])', text, re.I):
            matches.append((match.start(), match.end(), identity))
    return sorted(matches)


def _pure_status(text):
    if not re.search(PATTERNS['status'], text):
        return False
    return not re.search(r'评估|资格|条件|能[不能否]*退|能换|能否.{0,4}(?:退|换|修)|想退|要退|想换|要换|(?:申请|办理|请)(?:退货|退款|换货|补发|维修|保修)|(?:维修|保修|退货|退款|换货|补发)(?:方式|流程|怎么)|(?:怎么|如何)(?:退|换|修|申请|办理)', text)


def _status_subjects(text):
    """Finite query objects; querying an existing record is not a new remedy."""
    if not re.search(PATTERNS['status'], text): return []
    subjects = []
    if re.search(r'签收|物流|配送|快递|发货', text): subjects.append('delivery')
    if re.search(r'退款|到账', text): subjects.append('refund')
    if re.search(r'维修|保修|返修', text): subjects.append('repair')
    if re.search(r'售后(?:申请)?记录|售后状态', text): subjects.append('after_sale_records')
    return subjects or ['status']


def _requested_order_ids(text, orders=()):
    """Keep customer-supplied unknown identifiers separate from owned IDs."""
    names = [(m[0], m[2]) for m in _order_mentions(text, orders)]
    for pattern in (r'(?<![a-z0-9_-])(?:DEMO|EVAL|ORDER)[a-z0-9_-]+(?![a-z0-9_-])',
                    r'订单\s*[:：]?\s*([a-z0-9][a-z0-9_-]{2,})'):
        for match in re.finditer(pattern, text, re.I):
            names.append((match.start(), match.group(1) if match.lastindex else match.group()))
    return list(dict.fromkeys(name for _, name in sorted(names)))


def _after_sale_segments(text, orders, query_kinds=()):
    # A conjunction before another request is a clause boundary. An order-only
    # coordinated subject (A和B都维修) remains one group sharing one request.
    def has_request(part):
        return any(re.search(PATTERNS[kind], part) for kind in query_kinds) if query_kinds else bool(request_types(part))
    fragments = re.split(r'[，,。；;！？?\n]+|另外|另问|顺便|同时|以及|而且|但是|不过|并(?:且)?(?=(?:申请|办理|想|要|查询|查|咨询))|(?:和|与|及)(?!(?:退货|退款|维修|保修|换货|补发|物流)(?:的)?(?:状态|进度|到账))(?=(?:退货|退款|维修|保修|换货|补发|物流))', text)
    for fragment in fragments:
        fragment = fragment.strip()
        if not fragment:
            continue
        mentions = _order_mentions(fragment, orders)
        starts = [0]
        for index in range(1, len(mentions)):
            previous, current = mentions[index-1], mentions[index]
            if has_request(fragment[starts[-1]:current[0]]):
                # Preserve an action placed before the next order, e.g.
                # "A维修和退货B", rather than borrowing it for A.
                between = fragment[previous[1]:current[0]]
                conjunctions = list(re.finditer(r'和|与|及', between))
                split_at = current[0]
                if conjunctions:
                    candidate = previous[1] + conjunctions[-1].start()
                    if has_request(fragment[candidate:current[0]]): split_at = candidate
                starts.append(split_at)
        for start, end in zip(starts, [*starts[1:], len(fragment)]):
            yield fragment[start:end].strip(' 和与及')


def _has_unknown_order(text, orders):
    known = {o['id'].lower() for o in orders}
    named = re.findall(r'(?<![a-z0-9_-])(?:DEMO|EVAL|ORDER)[a-z0-9_-]+(?![a-z0-9_-])', text, re.I)
    named += re.findall(r'订单\s*[:：]?\s*([a-z0-9][a-z0-9_-]{2,})', text, re.I)
    return any(identity.lower() not in known for identity in named)


def _shared_readonly_assessment(clause):
    """Explicit per-order advice, never implicit draft or execution authority."""
    request = re.search(r'(分别|各自|逐单).{0,8}(?:判断|评估|核对|核实)', clause)
    postposed = False
    if not request:
        request = re.search(r'(?:建议|结论|结果)\s*(分别|各自|逐单)\s*(?:是|有|如何|怎样)', clause)
        postposed = bool(request)
    if not request:
        return False
    if re.search(r'如果|假如|假设|要是|倘若|朋友|别人|举例|引用|[“”「」\"]', clause):
        return False
    # The 别 in 分别 is part of the positive collective marker.
    before = clause[:request.start()]
    action = request.group()[len(request.group(1)):]
    denial = (r'(?:别|不要|不用|无需|不必|不需要|没有要求|未要求|不是要)' if postposed
              else r'(?:不|别|不要|不用|无需|不必|没有要求|未要求|不是要)')
    if (re.search(denial + r'.{0,8}$', before)
            or re.search(r'(?:不|别|不要|不用|无需|不必).{0,4}(?:判断|评估|核对|核实)', action)):
        return False
    # Do not propagate a collective clause containing a write action, even
    # when it also says "assess". Existing per-order authorization still runs.
    return not bool(re.search(r'创建|建立|整理|起草|拟|提交|登记|执行|办理|申请', clause))


def _order_fact_segment(clause):
    """A supplied condition/event can name a subject without naming a remedy."""
    if re.search(r'如果|假如|假设|要是|倘若|朋友|别人|举例|引用|[“”「」\"]', clause):
        return False
    return bool(re.search(r'摔|损坏|破损|坏|故障|不开机|无法开机|失灵|没声音|正常使用|'
                          r'完好|配件齐全|开箱|收到|签收|少发|漏发|少装|漏装|缺件|'
                          r'未洗|未穿|仅试|不喜欢|不合身|缩水|洗涤|误用|使用不当|责任|原因', clause))


def _collective_assessment_request(text):
    """An affirmative two-order callback grants advice only, never a write."""
    if not re.search(r'两(?:笔|个)?(?:订单|单)|两笔|两个订单', text):
        return False
    # A refusal to create an application can qualify the assessment in the
    # same clause. Remove only that refusal when checking for a positive read.
    read = re.sub(r'(?:先|暂时)?(?:别|不要|不用|不必|暂不|先不)'
                  r'(?:再|替我|帮我)?(?:创建|建立|建|整理|起草|提交|登记)'
                  r'(?:售后)?(?:申请|草稿)', '', text)
    return _shared_readonly_assessment(read) and bool(request_types(read))


def recalled_assessment_orders(text, messages, products, orders):
    """Resolve an explicitly renewed pair from owned customer source rows.

    Product FAQs do not erase an old service group. A third actual order event
    makes a bare "two orders" ambiguous until the customer names a new pair.
    Neither assistant summaries nor an account merely having two orders is a
    source for this callback. Facts themselves remain separate per order.
    """
    if not _collective_assessment_request(text) or _has_unknown_order(text, orders):
        return []
    owned = {order['id'] for order in orders}
    named = list(dict.fromkeys(m[2] for m in _order_mentions(text, orders)))
    if len(named) == 2:
        return named
    if len(named) > 2:
        return []
    seen, pair = [], []
    for message in messages:
        if message.get('role') != 'user' or message.get('status') in ('failed', 'cancelled'):
            continue
        source = message.get('content', '')
        if re.search(r'朋友|别人|替他|替她|他说|她说|如果|假如|假设|举例|引用|[“”「」『』\"]', source):
            continue
        if _has_unknown_order(source, orders) and (_order_fact_segment(source) or request_types(source)):
            seen, pair = [None], []
            continue
        supplied, focus = [], []
        for clause in _after_sale_segments(source, orders):
            refs = list(dict.fromkeys(m[2] for m in _order_mentions(clause, orders)))
            if refs:
                focus = refs
            elif _has_unknown_order(clause, orders):
                focus = []
            elif pids := _products(clause, products):
                bound = {order.get('product_id') for order in orders if order['id'] in focus}
                if bound and not bound.intersection(pids):
                    focus = []
            if _pure_status(clause) or not (_order_fact_segment(clause) or request_types(clause)):
                continue
            supplied.extend(oid for oid in focus if oid in owned and oid not in supplied)
        if len(supplied) == 2:
            seen, pair = list(supplied), list(supplied)
        elif supplied:
            seen = list(dict.fromkeys([*seen, *supplied]))
            pair = list(seen) if len(seen) == 2 and None not in seen else []
    return pair if set(named).issubset(pair) else []


def _after_sale_intents(text, products, orders, selected_order):
    mentions = list(dict.fromkeys(m[2] for m in _order_mentions(text, orders)))
    # One unambiguous explicit order may appear after its request. With several
    # orders, never manufacture every type/order combination.
    current_orders = mentions if len(mentions) == 1 else [] if mentions or _has_unknown_order(text, orders) else ([selected_order] if selected_order else [])
    order_products = {o['id']: o.get('product_id') for o in orders}
    current_products = []
    result = []
    pending_fact_orders = []
    named_fact_focus = []
    for clause in _after_sale_segments(text, orders):
        explicit_orders = list(dict.fromkeys(m[2] for m in _order_mentions(clause, orders)))
        pids = _products(clause, products)
        unknown_order = _has_unknown_order(clause, orders)
        types = request_types(clause)
        prior_products = set(current_products) | {order_products[o] for o in current_orders if order_products.get(o)}
        if explicit_orders:
            current_orders = [] if unknown_order else explicit_orders
            current_products = pids or list(dict.fromkeys(order_products[o] for o in current_orders if order_products.get(o)))
        elif unknown_order:
            current_orders = []
            current_products = pids
        elif pids:
            bound_products = set(current_products) | {order_products[o] for o in current_orders if order_products.get(o)}
            if bound_products and not bound_products.intersection(pids): current_orders = []
            current_products = pids
        shared_scope = (not explicit_orders and not pids and not unknown_order and types
                        and len(pending_fact_orders) > 1 and _shared_readonly_assessment(clause)
                        and not _pure_status(clause))
        if shared_scope:
            # Facts are not copied between orders. Only the explicitly shared
            # read-only remedy receives the union of the preceding subjects.
            current_orders = list(pending_fact_orders)
            current_products = list(dict.fromkeys(order_products[o] for o in current_orders if order_products.get(o)))
            pending_fact_orders, named_fact_focus = [], []
        elif unknown_order or _withdrawal_clause(clause) or types or _pure_status(clause):
            pending_fact_orders, named_fact_focus = [], []
        elif pids and not explicit_orders and prior_products and not prior_products.intersection(pids):
            pending_fact_orders, named_fact_focus = [], []
        elif explicit_orders:
            named_fact_focus = list(explicit_orders)
            if _order_fact_segment(clause):
                pending_fact_orders = list(dict.fromkeys([*pending_fact_orders, *named_fact_focus]))
            else:
                # A bare order heading can be followed by its fact clause.
                remainder = clause
                for oid in explicit_orders: remainder = re.sub(re.escape(oid), '', remainder, flags=re.I)
                if re.sub(r'订单|[\s：:]+', '', remainder):
                    pending_fact_orders, named_fact_focus = [], []
        elif named_fact_focus and _order_fact_segment(clause):
            pending_fact_orders = list(dict.fromkeys([*pending_fact_orders, *named_fact_focus]))
        else:
            # Price/stock/FAQ/new-shopping clauses break adjacency even when
            # they do not contain another exact product or order identity.
            pending_fact_orders, named_fact_focus = [], []
        if _pure_status(clause):
            continue
        if not types and (_withdrawal_clause(clause) or re.search(r'(?:不|不要|不用|不需要|不是(?:要|想)?|并非(?:要|想)?)(?:再)?(?:申请|办理)?(?:退货|退款|维修|换货|补发)', clause)):
            continue
        if not types and not re.search(PATTERNS['after_sale'], clause):
            continue
        for request_type in types or [None]:
            item = {'kind':'after_sale','request_type':request_type,'product_ids':list(current_products),
                    'order_ids':list(current_orders),'text':clause}
            if shared_scope: item['assessment_scope'] = 'separate_owned_fact_clauses'
            result.append(item)
    # Delivery circumstances can support a return/reship without requesting a
    # separate investigation. Keep independently requested logistics work.
    remedy_scopes = {(tuple(i['order_ids']), tuple(i['product_ids'])) for i in result
                     if i['request_type'] and i['request_type'] != 'logistics'}
    result = [i for i in result if i['request_type'] != 'logistics'
              or (tuple(i['order_ids']), tuple(i['product_ids'])) not in remedy_scopes
              or _explicit_logistics_action(i['text'])]
    # A fault description preceding its explicit remedy is supporting context,
    # not an additional unresolved generic after-sale request.
    typed_scopes = {(tuple(i['order_ids']), tuple(i['product_ids'])) for i in result if i['request_type']}
    for item in result:
        if item.get('assessment_scope') == 'separate_owned_fact_clauses':
            for oid in item['order_ids']:
                typed_scopes.add(((oid,), (order_products[oid],) if order_products.get(oid) else ()))
    result = [i for i in result if i['request_type'] or (tuple(i['order_ids']), tuple(i['product_ids'])) not in typed_scopes]
    unique = {}
    for item in result:
        key = (item['request_type'], tuple(item['order_ids']), tuple(item['product_ids']))
        if key in unique:
            unique[key]['text'] += '；' + item['text']
        else:
            unique[key] = item
    return list(unique.values())


def _intent_request_type(intent):
    if intent.get('request_type') in _REQUEST_TYPES:
        return intent['request_type']
    # Old JSON remains readable. Only a single explicit historical remedy may
    # be used to continue an old ID; never collapse an old multi-remedy item.
    types = request_types(intent.get('text', '')) if intent.get('kind') == 'after_sale' else []
    return types[0] if len(types) == 1 else None


def _same_intent(previous, current):
    if previous.get('kind') != current.get('kind'):
        return False
    old_orders, new_orders = sorted(previous.get('order_ids', [])), sorted(current.get('order_ids', []))
    if old_orders != new_orders:
        return False
    if current['kind'] == 'status':
        old_subjects = [previous['query_subject']] if previous.get('query_subject') else _status_subjects(previous.get('text', ''))
        new_subjects = [current['query_subject']] if current.get('query_subject') else _status_subjects(current.get('text', ''))
        if old_subjects != new_subjects: return False
        if not new_orders:
            old_requested = previous.get('requested_order_ids')
            if old_requested is None:
                old_requested = _requested_order_ids(previous.get('text', ''))
                # Old ambiguous text cannot establish one inferred subject.
                if len(old_requested) > 1: return False
            if old_requested != current.get('requested_order_ids', []): return False
    if previous.get('request_key') or current.get('request_key'):
        if previous.get('request_key') != current.get('request_key'): return False
        if current.get('condition') or previous.get('condition'):
            return _question_key(previous.get('condition', {}).get('subject_text','')) == _question_key(current.get('condition', {}).get('subject_text',''))
        if current.get('request_key') == 'extra_gift':
            old_object=previous.get('request_object',{}).get('text')
            new_object=current.get('request_object',{}).get('text')
            if not old_object or not new_object:
                return _question_key(previous.get('text','')) == _question_key(current.get('text',''))
            if old_object != new_object:return False
        return sorted(previous.get('product_ids', [])) == sorted(current.get('product_ids', []))
    if current['kind'] == 'question' and _question_key(previous.get('text', '')) != _question_key(current.get('text', '')):
        # Two unfamiliar questions about the same product are independent work.
        # Derive from text so old JSON remains readable without a migration.
        return False
    if current['kind'] == 'after_sale':
        if _intent_request_type(previous) != current.get('request_type'):
            return False
        # Explicit order identity already determines its purchased product.
        # Old records did not copy that product ID into the derived intent.
        if new_orders:
            return True
    return sorted(previous.get('product_ids', [])) == sorted(current.get('product_ids', []))


def _question_key(text):
    return re.sub(r'[\s，,。；;！？?]+', '', text).casefold()


def _residual_questions(text, products, orders):
    """Retain uncovered positive requests with their local owned subjects."""
    result, seen = [], set()
    current_orders, current_products = [], []
    order_products = {o['id']:o.get('product_id') for o in orders}
    blocked = False
    previous_clause = ''
    for _, _, clause in request_fragments.clauses(text):
        preceding_clause, previous_clause = previous_clause, clause
        explicit_orders = list(dict.fromkeys(m[2] for m in _order_mentions(clause, orders)))
        named = _products(clause, products)
        if explicit_orders or _has_unknown_order(clause, orders):
            current_orders = [] if _has_unknown_order(clause, orders) else explicit_orders
            current_products = named or [order_products[o] for o in current_orders if order_products.get(o)]
        elif named:
            current_products, current_orders = named, []
        if re.search(request_fragments.UNASSERTED, clause): blocked = True
        if re.search(r'我(?:自己|本人)|现在(?:明确|决定)|实际要求', clause): blocked = False
        question = re.search(request_fragments.QUESTION, clause)
        directive = re.search(request_fragments.DIRECTIVE, clause)
        if not question and not directive: continue
        if (not named and not explicit_orders
                and re.fullmatch(r'(?:请|麻烦)?(?:给我|提供|给)(?:一个|一份)?(?:明确|具体|详细|清楚)?(?:建议|结论|答复|答案|解释|回复)[？?]?',clause)
                and (request_types(preceding_clause) or any(re.search(pattern,preceding_clause) for pattern in PATTERNS.values()))):
            continue
        if (blocked and not question) or re.search(request_fragments.NEGATIVE, clause): continue
        if (_withdrawal_clause(clause)
                or re.search(r'(?:不用|不要|不需要|不必|别)(?:再|分别)?(?:告诉|介绍|说明|回答|问|说说|说|列)|不想(?:要)?(?:了解|知道)', clause)
                or any(re.search(pattern, clause) for pattern in PATTERNS.values()) or request_types(clause)):
            continue
        key = _question_key(clause)
        if key in seen: continue
        seen.add(key)
        item = {'kind':'question','product_ids':list(current_products),'order_ids':list(current_orders),'text':clause}
        if not blocked and not re.search(r'政策|规则|一般', clause) and re.search(r'额外|加送|多要|赠送|赠品', clause) and re.search(r'要|想|希望|请|能|可以|免费|赠送|加送', clause):
            item['request_key'] = 'extra_gift'
            item['request_object'] = request_fragments.gift_object(clause, preceding_clause)
        result.append(item)
    return result


def _conditional_intents(text, products):
    result=[]
    for span in request_fragments.conditional_recommendations(text):
        constraints = {k:v for k,v in changes_from_text('推荐'+span['body'], products).items() if k in KEYS}
        result.append({'kind':'recommend','product_ids':[],'order_ids':[],'text':span['text'],
                       'request_key':'conditional_alternative', 'condition':span['condition'],
                       'constraints':constraints,'scope':_scope(span['condition']['subject_text']+' '+span['body'],products,'会话')})
    return result


def _query_intents(text, kinds, products, orders, selected_order):
    """Bind each query to its local product/order, carrying comma continuations.

    A later after-sale order is not the subject of an earlier shopping price.
    Coordinated products inside one comparison remain a single query.
    """
    order_products = {o['id']: o.get('product_id') for o in orders}
    current_products, current_orders, unknown_order, requested_orders = [], [], False, []
    result = {}
    for clause in _after_sale_segments(text, orders, kinds):
        clause = clause.strip()
        if not clause:
            continue
        pids = _products(clause, products)
        oids = list(dict.fromkeys(m[2] for m in _order_mentions(clause, orders)))
        unknown = _has_unknown_order(clause, orders)
        if oids or unknown:
            unknown_order = unknown
            requested_orders = _requested_order_ids(clause, orders)
            current_orders = [] if unknown else oids
            current_products = pids or list(dict.fromkeys(order_products[o] for o in current_orders if order_products.get(o)))
        elif pids:
            current_products, current_orders, unknown_order = pids, [], False
            requested_orders = []
        elif (any(re.search(PATTERNS[kind], clause) for kind in kinds)
              and _scope(clause, products, '会话') != '会话'
              and not request_types(clause)):
            # An explicitly named category is a new catalogue subject. It may
            # have no exact model, and must not borrow a preceding order.
            current_products, current_orders, unknown_order = [], [], False
            requested_orders = []
        for kind in kinds:
            if not re.search(PATTERNS[kind], clause):
                continue
            bound_orders = list(current_orders)
            bound_products = list(current_products)
            if kind == 'status' and not bound_orders and not bound_products and not unknown_order and selected_order:
                bound_orders = [selected_order]
                if order_products.get(selected_order): bound_products = [order_products[selected_order]]
            for subject in _status_subjects(clause) if kind == 'status' else [None]:
                key = (kind, subject, tuple(bound_products), tuple(bound_orders), tuple(requested_orders) if kind == 'status' else ())
                if key in result:
                    result[key]['text'] += '；' + clause
                else:
                    result[key] = {'kind': kind, 'product_ids': bound_products, 'order_ids': bound_orders, 'text': clause}
                    if kind == 'status':
                        result[key].update(query_subject=subject, requested_order_ids=list(requested_orders or bound_orders))
    return list(result.values())


_ORDER_REFERENCE = re.compile(r'(?:回到|回看|再说|继续(?:说|问|查)?)(?:我)?(?:刚才|之前|前面|最早|原来)?(?:那|这|原)(?:笔)?(?:订单|单)|(?:现在)?(?:按|根据)(?:我)?(?:最早|刚才|之前|前面|原来)(?:说过|说的|纠正过|更正过|说明过)(?:的)?(?:原因|实际状态|收货情况)')


def _reference_blocks(text):
    # A comma may join a callback to its action; an explicit new question or
    # sentence ends that scope. Keep the original wording for provenance.
    return [part.strip(' ，,') for part in re.split(r'[。；;\n]|另外|另问|顺便|同时|还有',text)
            if re.search(r'[^\W_]',part)]


def detect_intents(text, products=(), orders=(), selected_order=None, *, history=()):
    # Mask only denied service actions; keep the original persisted message
    # and any independently asserted positive request intact.
    text = request_fragments.without_denied_service_requests(text)
    if re.fullmatch(r'\s*(?:谢谢|谢谢你|好的|好|嗯|知道了|收到|了解了|明白了)[，。！!\s]*', text): return []
    blocks = _reference_blocks(text)
    if _ORDER_REFERENCE.search(text) and len(blocks)>1:
        # Preserve an otherwise unfamiliar callback request even when another
        # block has a known product/query, without borrowing that product.
        return [item for block in blocks for item in detect_intents(block,products,orders,selected_order,history=history)]
    conditionals = _conditional_intents(text, products)
    text = request_fragments.mask_spans(text, request_fragments.conditional_recommendations(text))
    # Masking retains separators; punctuation alone is not another request.
    if not re.search(r'[^\W_]', text): return conditionals
    kinds = [kind for kind, pattern in PATTERNS.items() if kind != 'after_sale' and re.search(pattern, text)]
    if 'manual' in kinds and re.search(r'不要|不需要|不用|别', text) and not re.search(r'(?:现在|还是|改为)(?:请)?(?:转|找|接入)?人工', text): kinds.remove('manual')
    pids = _products(text, products)
    oids = list(dict.fromkeys(m[2] for m in _order_mentions(text, orders)))
    after_sale = _after_sale_intents(text, products, orders, selected_order)
    recalled = recalled_assessment_orders(text, history, products, orders)
    if recalled:
        order_products = {order['id']: order.get('product_id') for order in orders}
        for item in after_sale:
            if item.get('request_type') and (
                    _collective_assessment_request(item.get('text', ''))
                    or _shared_readonly_assessment(item.get('text', ''))):
                item.update(order_ids=list(recalled),
                            product_ids=list(dict.fromkeys(order_products[oid] for oid in recalled if order_products.get(oid))),
                            assessment_scope='separate_owned_history')
    questions = _residual_questions(text, products, orders)
    if not kinds and not after_sale and not questions and text.strip(): kinds = ['question']
    queries = _query_intents(text, [k for k in kinds if k not in ('manual', 'question')], products, orders, selected_order)
    global_items = [{'kind':kind,'product_ids':pids,'order_ids':oids,'text':text} for kind in kinds if kind in ('manual', 'question')]
    return queries + global_items + after_sale + questions + conditionals


def _bind_explicit_order_reference(c, cid, intent, text, products, orders):
    """Resolve a resumed order only from one owned, customer-sourced subject."""
    if intent.get('order_ids') or intent.get('product_ids') or intent.get('condition'):return
    blocks=[block for block in _reference_blocks(text) if intent.get('text','').strip(' ，,。；;') in block]
    if len(blocks)!=1:return
    text=blocks[0]
    if _order_mentions(text,orders) or _has_unknown_order(text,orders) or _products(text,products):return
    unasserted=r'朋友|别人|替他|替她|他说|她说|你说|您说|如果|要是|若|假设|假如|比如|例如|举例|FAQ|例子|[“"].*[”"].*(?:这句话|意思|含义)' 
    if re.search(unasserted,text,re.I) or re.search(r'(?:不|别|不要|不用|不必)(?:再)?(?:回到|回看|再说|继续)',text):return
    reference=_ORDER_REFERENCE.search(text)
    if not reference:return
    owned={order['id']:order for order in orders};candidates={}
    for row in c.execute("SELECT * FROM service_intents WHERE conversation_id=? AND status!='withdrawn' ORDER BY rowid",(cid,)):
        data=json.loads(row['data']);sources=[]
        source_ids=list(dict.fromkeys([*data.get('source_message_ids',[]),row['source_message_id']]))
        for source_id in source_ids:
            source=c.execute("SELECT content FROM messages WHERE id=? AND conversation_id=? AND role='user'",(source_id,cid)).fetchone()
            if not source or re.search(unasserted,source['content'],re.I):continue
            # A previously discussed inaccessible order also makes "that order"
            # ambiguous; do not silently fall back to the only accessible one.
            if _has_unknown_order(source['content'],orders):return
            sources.append((source_id,source['content']))
        for oid in data.get('order_ids',[]):
            if oid not in owned:continue
            for source_id,source_text in sources:
                if oid not in {m[2] for m in _order_mentions(source_text,orders)}:continue
                candidates.setdefault(oid,{'source_message_id':source_id,'source_text':source_text})
                break
    if len(candidates)!=1:return
    oid=next(iter(candidates));pid=owned[oid].get('product_id')
    if not pid:return
    product=next((p for p in products if p['id']==pid),{})
    scope=product.get('category','会话')
    if _scope(text,products,'会话') not in ('会话',scope):return
    intent.update(order_ids=[oid],product_ids=[pid],scope=scope,
                  order_reference={'order_id':oid,**candidates[oid],'reference_text':reference.group(),
                                   'basis':'unique_owned_customer_source'})


def _constraint_source(source, source_text, version):
    return {'source_message_id':source,'source_text':source_text,'version':version,
            'source_kind':'customer_edit' if source.startswith('customer_edit:') else 'customer_message'}


def _update_conditional(c, state, record, source, changes=None, *, clear=False, withdraw=False, source_text=None):
    """Edit only named fields and preserve every other field's original source."""
    updates={key:None for key in record['constraints']} if clear else dict(changes or {})
    if source_text is None:
        if source.startswith('customer_edit:'):
            labels={'budget_max_cents':'预算上限','colors':'颜色','excluded_colors':'排除颜色','sizes':'尺码','excluded_sizes':'排除尺码','usage':'用途','required_features':'必需功能','excluded_features':'排除功能','excluded_products':'排除型号'}
            details=[]
            for key,value in updates.items():
                label=labels[key]
                if value is None or value==[]:details.append('移除'+label);continue
                display=(format(Decimal(value)/100,'f').rstrip('0').rstrip('.') if value%100 else str(value//100))+'元' if key=='budget_max_cents' else '、'.join(value) if isinstance(value,list) else value
                details.append('将'+label+'更正为'+display)
            source_text='顾客在选购要求面板'+('移除这条条件要求' if clear else '；'.join(details))
        else:
            message=c.execute("SELECT content FROM messages WHERE id=? AND role='user'",(source,)).fetchone()
            source_text=message['content'] if message else ''
    record.setdefault('original_source_text',record.get('source_text',''))
    sources=record.setdefault('constraint_sources',{})
    for key in record['constraints']:
        sources.setdefault(key,_constraint_source(record.get('source_message_id',''),record.get('source_text',''),record.get('version',0)))
    for key in updates:sources[key]=_constraint_source(source,source_text,state['version']+1)
    record['constraints'].update(updates)
    record.update(active=not clear and any(value is not None and value != [] for value in record['constraints'].values()),
                  source_message_id=source,source_text=source_text,version=state['version']+1)
    row = c.execute('SELECT * FROM service_intents WHERE id=?', (record['id'],)).fetchone()
    if row:
        payload = json.loads(row['data'])
        payload.update(constraints=dict(record['constraints']),constraint_sources=sources,constraints_active=record['active'],
                       constraints_version=record['version'],constraints_source_message_id=source)
        c.execute('UPDATE service_intents SET data=?,status=?,updated_at=? WHERE id=?',
                  (_dump(payload),'withdrawn' if withdraw else row['status'],db.now(),record['id']))


def _conditional_controls(c, state, text, products, source):
    spans=[];changed=False
    records=state.get('conditional_requests',[])
    for start,end,clause in request_fragments.clauses(text):
        # A reference to an existing requirement is not a reported historical
        # wish. Other third-party, hypothetical or quoted clauses stay inert.
        control_clause=re.sub(r'(?:之前|以前|原来)(?=的?条件)', '', clause)
        if re.search(request_fragments.UNASSERTED,control_clause) or re.search(r'FAQ|资料|说明.{0,4}(?:说|写)',control_clause,re.I): continue
        drop_condition = bool(re.search(r'(?:不用|无需|不必)(?:再)?等|取消(?:之前|原来|那个|这条|这个)?(?:的)?条件|(?:不管|无论).{0,20}(?:有无|有没有|是否有货)',clause))
        if not drop_condition and re.search(r'吗|么|是否|能否|怎么|如何',clause):continue
        referenced = bool(re.search(r'替代|备选|(?:这个|那个|刚才|之前|原来)的?条件',clause))
        bare_budget = any(r['active'] for r in records) and bool(re.fullmatch(r'\s*(?:我的|现在|这次)?预算[^。；;]{0,30}',clause)) and not any(i.get('active') and i['key']=='budget_max_cents' for i in state['items'])
        if not (referenced or drop_condition or bare_budget): continue
        candidates=[r for r in records if r['active'] or referenced]
        named=[r for r in candidates if r['condition']['subject_text'].casefold() in clause.casefold()]
        if named:candidates=named
        # Even ambiguous edits must not leak into unconditional preferences.
        spans.append({'start':start,'end':end})
        if len(candidates)!=1:continue
        record=candidates[0]
        cancel=bool(re.search(r'(?:取消|撤回|不要|不用|不再).{0,6}(?:替代|备选)|(?:替代|备选).{0,6}(?:不用了|不要了|取消)',clause))
        changes={k:v for k,v in changes_from_text(control_clause,products).items() if k in KEYS}
        if cancel or drop_condition:
            _update_conditional(c,state,record,source,clear=True,withdraw=cancel);changed=True
        elif changes:
            _update_conditional(c,state,record,source,changes);changed=True
    return request_fragments.mask_spans(text,spans),changed,spans


def _validate_preference_value(key, value):
    if key not in KEYS: raise HTTPException(422, '不支持的选购要求')
    if value is None:return
    if key == 'budget_max_cents' and (type(value) is not int or not 0 <= value <= 100000000): raise HTTPException(422, '预算须为有效金额')
    if key == 'usage' and (not isinstance(value, str) or len(value) > 120): raise HTTPException(422, '用途过长')
    if key in LIST_KEYS and (not isinstance(value, list) or len(value) > 20 or any(not isinstance(v, str) or len(v) > 60 for v in value)): raise HTTPException(422, '选购条件须为简短列表')


def sync(c, conv, products):
    cid = conv['id']; state = _load(c, cid)
    rows = c.execute("SELECT rowid AS seq,* FROM messages WHERE conversation_id=? AND role='user' AND rowid>? ORDER BY rowid", (cid, state['processed_rowid'])).fetchall()
    orders = [{'id': r[0], 'product_id': json.loads(r[1]).get('product_id')} for r in c.execute('SELECT id,data FROM orders WHERE user_id=?', (conv['user_id'],))]
    for row in rows:
        text = row['content']; previous_scope = state['scope']; changed = False
        preference_text = request_fragments.mask_spans(text, request_fragments.conditional_recommendations(text))
        preference_text, conditional_changed, conditional_control_spans = _conditional_controls(c,state,preference_text,products,row['id'])
        changed = changed or conditional_changed
        segments = []
        for block, target in _order_preference_blocks(preference_text, products, orders, conv.get('selected_order_id')):
            owned_product_scope = next((p.get('category', '会话') for p in products if p['id'] == target), None)
            block_scope = owned_product_scope or (segments[-1][0] if segments else state['scope'])
            # An owned order fixes its product category. A negated reference
            # to the prior item ("not the clothing") cannot override that binding.
            segments.extend((owned_product_scope or scope, segment, target) for scope, segment in _preference_segments(block, products, block_scope, None if target is False else conv.get('selected_product_id')))
        scope = segments[-1][0]
        control_text = '，'.join(clause for clause in _asserted_clauses(text)
                                if not re.search(r'(?:别|不要|不用|不需要|不想)(?:再)?(?:清除|忘掉)|怎么|如何|能不能|是否|吗', clause))
        if re.search(r'清除(?:全部|所有|之前|我的|这些|本次)?(?:的)?(?:偏好|选购要求)|忘掉(?:之前|所有)(?:的)?(?:偏好|要求)', control_text):
            for item in list(state['items']): _set(state, item['key'], None, item['scope'], row['id'], item.get('product_id'), text)
            for record in state.get('conditional_requests',[]): _update_conditional(c,state,record,row['id'],clear=True)
            state['shopping'] = {'selections': [], 'goal': None, 'source_message_id': row['id']}
            changed = True
        else:
            for segment_scope, segment, target in segments:
                segment_changed = _apply_preference_segment(state, segment, products, segment_scope, row['id'], target)
                changed = changed or segment_changed
            shopping = shopping_state.update(state.get('shopping'), '，'.join(_asserted_clauses(preference_text)), products, row['id'], _products)
            # Corrections to an existing choice must not leave its old variants
            # active behind the new preference. Conditional alternatives never
            # turn into a choice through this synchronization.
            if not re.search(r'若不能|否则|如果不行|(?:不行|不能)(?:的话|就)|不能.{0,5}时', text):
                for selection in shopping.get('selections', []):
                    product = next((p for p in products if p['id'] == selection['product_id']), {})
                    for item in effective_items({'items':state['items']}, product):
                        if item.get('source_message_id') != row['id']: continue
                        key, value = item['key'], item['value']
                        field = 'colors' if key in ('colors', 'excluded_colors') else 'sizes' if key in ('sizes', 'excluded_sizes') else None
                        if not field: continue
                        if key == field: selection[field] = list(value)
                        else:
                            normalize = shopping_state.color_key if field == 'colors' else str.upper
                            excluded = {normalize(v) for v in value}
                            selection[field] = [v for v in selection.get(field, []) if normalize(v) not in excluded]
                        selection.update(source_message_id=row['id'], source_text=item.get('source_text', text))
            changed = changed or shopping != state.get('shopping')
            state['shopping'] = shopping
        withdrawal = _withdraw_intents(c, cid, text, row['id'], products, orders)
        intent_input = request_fragments.mask_spans(text,conditional_control_spans)
        intent_text = '，'.join(part for part in re.split(r'[，,。；;\n]|另外|同时|但是|但', intent_input) if part.strip() and not _withdrawal_clause(part)) if withdrawal else intent_input
        history = [dict(source) for source in c.execute(
            "SELECT role,content,status FROM messages WHERE conversation_id=? AND rowid<? ORDER BY rowid",
            (cid, row['seq']))] if re.search(r'两(?:笔|个)?(?:订单|单)|两笔|两个订单', intent_text) else []
        for intent in detect_intents(intent_text, products, orders, conv.get('selected_order_id'), history=history):
            _bind_explicit_order_reference(c,cid,intent,text,products,orders)
            prior = None
            if intent.get('request_key')=='extra_gift' and not intent['order_ids'] and re.search(r'刚才|前面|继续|那根|那个|那件|这根',intent['text']):
                preceding=[json.loads(x['data']) for x in c.execute("SELECT data FROM service_intents WHERE conversation_id=? AND status!='withdrawn' AND resolution!='solved'",(cid,))]
                preceding=[x for x in preceding if x.get('request_key')=='extra_gift']
                if len(preceding)==1:
                    intent['order_ids']=preceding[0].get('order_ids',[])
                    intent['product_ids']=preceding[0].get('product_ids',[])
                    if not intent.get('request_object',{}).get('text'):intent['request_object']=preceding[0].get('request_object',{})
            for candidate in c.execute("SELECT * FROM service_intents WHERE conversation_id=? ORDER BY rowid DESC", (cid,)):
                payload = json.loads(candidate['data'])
                if candidate['status'] not in ('pending','needs_info') and not ((intent.get('request_key') or intent.get('order_reference')) and candidate['status'] in ('answered','handed_off') and candidate['resolution'] != 'solved'): continue
                if _same_intent(payload, intent):
                    prior = candidate; break
            iid = prior['id'] if prior else db.uid('intent')
            old = json.loads(prior['data']) if prior else {}
            source_texts = old.get('source_texts', [])
            if not source_texts and old.get('latest_message_id') and old.get('text'):
                source_texts = [{'message_id': old['latest_message_id'], 'text': old['text']}]
            categories = {p.get('category', '会话') for p in products if p['id'] in intent.get('product_ids', [])}
            intent_scope = intent.get('scope') or (next(iter(categories)) if len(categories) == 1 else '会话' if categories else scope)
            intent.update(id=iid, source_message_ids=list(dict.fromkeys([*old.get('source_message_ids', []), row['id']])),
                          source_texts=[*source_texts, {'message_id': row['id'], 'text': text}], latest_message_id=row['id'], scope=intent_scope)
            if intent.get('condition'):
                intent['condition']['source_message_id'] = row['id']
                record = {key:intent[key] for key in ('id','scope','condition','constraints')}
                record.update(source_message_id=row['id'],source_text=text,original_source_text=text,version=state['version']+1,active=True)
                record['constraint_sources']={key:_constraint_source(row['id'],text,record['version']) for key in record['constraints']}
                state['conditional_requests'] = [i for i in state.get('conditional_requests',[]) if i['id'] != iid]+[record]
                intent.update(constraints_active=True,constraints_version=record['version'],constraints_source_message_id=row['id'],constraint_sources=record['constraint_sources'])
                changed = True
            c.execute("INSERT INTO service_intents VALUES (?,?,?,'pending','unknown',?,?) ON CONFLICT(id) DO UPDATE SET source_message_id=excluded.source_message_id,status='pending',resolution='unknown',data=excluded.data,updated_at=excluded.updated_at", (iid, cid, row['id'], _dump(intent), db.now()))
        if changed or scope != previous_scope: state['version'] += 1
        state['scope'] = scope; state['processed_rowid'] = row['seq']
    if rows: _save(c, cid, state)
    return public(state)


def safe_sync(c, conv, products):
    """Parser errors cannot roll back a customer's already-persisted message."""
    c.execute('SAVEPOINT preference_sync')
    try:
        result = sync(c, conv, products)
    except Exception as exc:
        c.execute('ROLLBACK TO preference_sync')
        c.execute('RELEASE preference_sync')
        log.warning('Preference sync failed: type=%s', type(exc).__name__)
        try:
            result = public(_load(c, conv['id']))
        except Exception:
            result = {'version': 0, 'items': [], 'scope': '会话', 'updated_at': None}
        return {**result, 'sync_error': True}
    c.execute('RELEASE preference_sync')
    return result


def intents(c, cid):
    return [{**json.loads(r['data']), 'source_message_id': r['source_message_id'], 'status': r['status'], 'resolution': r['resolution'], 'updated_at': r['updated_at']}
            for r in c.execute('SELECT * FROM service_intents WHERE conversation_id=? ORDER BY rowid', (cid,))]


def patch(user, cid, data):
    from . import service
    service.require_customer(user); request_id = service.request_key(data)
    with db.transaction() as c:
        conv = service._conversation(c, user, cid)
        cached = service.memo(c, user, 'preferences:' + cid, request_id)
        if cached: return cached
        sync(c, conv, service._products(c)); state = _load(c, cid)
        if type(data.get('expected_version')) is not int or data['expected_version'] != state['version']:
            raise HTTPException(409, '选购要求已更新，请刷新后再修改')
        changes = data.get('changes', [])
        if not isinstance(changes, list) or len(changes) > 30: raise HTTPException(422, '选购要求格式不正确')
        if data.get('clear_all'):
            for record in state.get('conditional_requests',[]): _update_conditional(c,state,record,'customer_edit:'+request_id,clear=True)
            changes = [{'key': i['key'], 'scope': i['scope'], 'product_id': i.get('product_id'), 'value': None} for i in state['items']]
            state['shopping'] = {'selections': [], 'goal': None, 'source_message_id': 'preference-clear:' + request_id}
        for change in changes:
            if not isinstance(change, dict): raise HTTPException(422, '选购要求格式不正确')
            key, value, scope = change.get('key'), change.get('value'), change.get('scope', state['scope'])
            product_id = change.get('product_id')
            target = next((p for p in service._products(c) if p['id'] == product_id), None) if isinstance(product_id, str) else None
            if product_id is not None and (not target or scope not in ('会话', target['category'])):
                raise HTTPException(422, '选购要求的商品范围不正确')
            if key not in KEYS or scope not in ('会话', '数码', '家电', '服饰', '家居'): raise HTTPException(422, '不支持的选购要求')
            _validate_preference_value(key,value)
            _apply_changes(state, {key: value}, scope, 'customer_edit:' + request_id, merge=False, product_id=product_id, overrides_global=bool(product_id))
            if key in ('colors', 'excluded_colors', 'sizes', 'excluded_sizes'):
                for selection in state.get('shopping', {}).get('selections', []):
                    product = next((p for p in service._products(c) if p['id'] == selection['product_id']), {})
                    if scope not in ('会话', product.get('category')) or product_id and selection['product_id'] != product_id: continue
                    field = 'sizes' if key in ('sizes', 'excluded_sizes') else 'colors'
                    normalize = str.upper if field == 'sizes' else shopping_state.color_key
                    if key == field:
                        selection[field] = value or []
                        selection.pop(field[:-1] + '_needs_confirmation', None)
                    elif value:
                        excluded = {normalize(option) for option in value}
                        previous = selection.get(field, [])
                        selection[field] = [option for option in previous if normalize(option) not in excluded]
                        if previous and not selection[field]: selection[field[:-1] + '_needs_confirmation'] = True
                    selection['source_message_id'] = 'customer_edit:' + request_id
                    selection.pop('source_text', None)
        conditional_changes = data.get('conditional_changes', [])
        if not isinstance(conditional_changes,list) or len(conditional_changes)>30: raise HTTPException(422,'条件选购要求格式不正确')
        for change in conditional_changes:
            if not isinstance(change,dict): raise HTTPException(422,'条件选购要求格式不正确')
            record=next((r for r in state.get('conditional_requests',[]) if r['id']==change.get('id')),None)
            if record is None: raise HTTPException(404,'条件选购要求不存在或无权访问')
            if change.get('clear') is True:
                if set(change)!={'id','clear'}:raise HTTPException(422,'条件选购要求格式不正确')
                _update_conditional(c,state,record,'customer_edit:'+request_id,clear=True)
            else:
                if set(change)!={'id','key','value'}:raise HTTPException(422,'条件选购要求格式不正确')
                _validate_preference_value(change['key'],change['value'])
                _update_conditional(c,state,record,'customer_edit:'+request_id,{change['key']:change['value']})
        state['version'] += 1; _save(c, cid, state)
        # Cancel before publishing any late reply based on the previous version.
        c.execute('UPDATE conversations SET generation=generation+1,updated_at=? WHERE id=?', (db.now(), cid))
        c.execute("UPDATE turns SET status='cancelled',phase='选购要求已更新，请继续咨询',updated_at=? WHERE conversation_id=? AND status='pending'", (db.now(), cid))
        service.audit(c, user, '修改本次选购要求', cid, None, public(state))
        return service.remember(c, user, 'preferences:' + cid, request_id, public(state))


def _unknown_order_query_answered(c, cid, data, source_id, answer_id):
    """Answered clarification is not a found order or a resolved service case."""
    if data.get('kind') != 'status' or data.get('order_ids'): return False
    refs = data.get('requested_order_ids', [])
    if len(refs) != 1: return False
    customer = c.execute("SELECT content FROM messages WHERE id=? AND conversation_id=? AND role='user'", (source_id, cid)).fetchone()
    answer = c.execute("SELECT content FROM messages WHERE id=? AND conversation_id=? AND role='assistant'", (answer_id, cid)).fetchone()
    owner = c.execute('SELECT user_id FROM conversations WHERE id=?', (cid,)).fetchone()
    if not customer or not answer or not owner: return False
    if refs[0] not in _requested_order_ids(customer['content']): return False
    if c.execute('SELECT 1 FROM orders WHERE user_id=? AND lower(id)=?', (owner['user_id'], refs[0].lower())).fetchone(): return False
    # Match an explicit current-account no-result answer, not an unrelated
    # failed lookup, a hypothetical quotation, or an invented successful status.
    pattern = r'(?:^|[。；;\n])\s*当前账号下未找到订单\s*' + re.escape(refs[0]) + r'(?![a-z0-9_-])'
    return bool(re.search(pattern, answer['content'], re.I))


def _prior_after_sale_reply_in_scope(c, cid, source_id, data, outcome):
    """An unresolved request is not evidence that this reply addressed it.

    Restrict a resumed request to the orders asked about now, then require a
    matching saved assessment/case or a clarification of that same remedy.
    The tool-name list alone cannot establish order or remedy identity.
    """
    targets = set(data.get('order_ids') or [])
    if not targets:
        return False
    current = [json.loads(row['data']) for row in c.execute(
        'SELECT data FROM service_intents WHERE conversation_id=? AND source_message_id=?',
        (cid, source_id))]
    current = [item for item in current if item.get('latest_message_id') == source_id
               and item.get('kind') in ('after_sale', 'status', 'question', 'manual')]
    current_orders = {oid for item in current for oid in item.get('order_ids', [])}
    if not targets.issubset(current_orders):
        return False
    answer = c.execute(
        "SELECT cards FROM messages WHERE id=? AND conversation_id=? AND role='assistant'",
        (outcome.get('answer_message_id'), cid)).fetchone()
    if not answer:
        return False
    remedy = _intent_request_type(data)
    covered = set()
    for card in json.loads(answer['cards'] or '[]'):
        if card.get('type') not in ('assessment', 'case'):
            continue
        payload = card.get('data') or {}
        subject = payload.get('subject') or (payload.get('assessment') or {}).get('subject') or {}
        order_id = payload.get('order_id') or subject.get('order_id')
        request_type = payload.get('request_type') or payload.get('type') or subject.get('type')
        if order_id in targets and (not remedy or request_type == remedy):
            covered.add(order_id)
    if targets.issubset(covered):
        return True
    # A same-remedy follow-up can ask for missing facts before an assessment
    # exists. It must not consume other remedies for the same order.
    if outcome.get('status') == 'needs_info':
        clarified = {oid for item in current if item.get('kind') == 'after_sale'
                     and (not remedy or _intent_request_type(item) == remedy)
                     for oid in item.get('order_ids', [])}
        return targets.issubset(clarified)
    return False


def record_outcomes(c, cid, source_id, outcomes):
    for outcome in outcomes:
        if outcome.get('status') not in ('answered', 'needs_info', 'handed_off', 'withdrawn'): continue
        row = c.execute('SELECT * FROM service_intents WHERE id=? AND conversation_id=?', (outcome.get('id'), cid)).fetchone()
        if not row: continue
        data = json.loads(row['data'])
        expected = outcome.get('expected_source_message_id', source_id)
        if data.get('latest_message_id') != expected: continue
        if (expected != source_id and data.get('kind') == 'after_sale'
                and not _prior_after_sale_reply_in_scope(c, cid, source_id, data, outcome)):
            continue
        data['answer_message_id'] = outcome.get('answer_message_id')
        data['answer_source_message_id'] = source_id
        data['answered_intent_source_message_id'] = expected
        data['evidence'] = outcome.get('evidence', [])
        status = outcome['status']
        if status == 'needs_info' and expected == source_id and _unknown_order_query_answered(c, cid, data, source_id, outcome.get('answer_message_id')):
            status = 'answered'
            data['query_result'] = 'order_not_found_for_current_customer'
        c.execute('UPDATE service_intents SET status=?,data=?,updated_at=? WHERE id=?', (status, _dump(data), db.now(), row['id']))
