"""Bounded catalogue record requests with one shared publication projection.

This does not interpret fit, recommend products, act on orders or infer facts.
The normal agent handles anything outside the fully recognized request. All
values come from this turn's fresh catalogue and later same-SKU tool results.
"""
from copy import deepcopy
import json
import re

from . import catalog_advice, publication_state
from .amounts import yuan


_COMPARISON = r'比较|对比|区别|差别|差异|不同'
_RECORD_MARKER = r'记录|登记|资料|参数|字段|标明|已知|未知'
_STRICT_RECORD = r'(?:按|读|查询|查|列出|列一下)(?:本店|店铺|本轮|当前)?(?:记录|资料)|(?:本店|店铺)(?:记录|资料)|记录参数|参数记录|字段'
_EXPLICIT_RECORD_FIELD = (r'已知记录|已(?:记录|登记)的?|明确(?:记录|登记)的?|标明的?|'
                          r'(?:记录|资料)(?:是什么|有哪些|有什么)|(?:有哪些|有什么)(?:已知)?(?:记录|资料)')
_OTHER_WORK = (
    r'订单|售后|退货|退款|换货|赔|物流|收货|签收|发票|人工|'
    r'如果|假如|假设|上次|曾经|历史|之前|以前|昨天|明天|以后|未来|保证|'
    r'推荐|适合|合身|怎么|如何|为何|为什么|哪个|哪款|谁|更|选购|购买|下单|'
    r'报价|实付|优惠|价格|多少钱|预算|开票|下载|上传|删除|修改|撤销|取消|'
    r'帮我买|我要买|帮我选|我想买|'
    r'不要(?:查|看|列|说)|不用(?:查|看|列|说)|无需(?:查|看|列|说)|(?<![分区差个])别(?:查|看|列|说)'
)
_NO_INFERENCE = (
    r'(?:不要|不用|无需|请勿|(?<![分区差个])别|不(?=猜|推测|推断|外推))(?:再)?(?:用[^，,。；;！？?\n]*?)?'
    r'(?:猜(?:测)?|推测|推断|外推)'
    r'(?:(?!也|还|然后|并|请|帮|顺便|另外|同时|而且|以及|能否|能不能|可否|可不可以|是否|有没有)[^，,。；;！？?\n])*'
)
_NO_TRANSFER = (r'(?:不要|别|请勿)把[\w\u4e00-\u9fff]{1,30}(?:当作|当|说成|作为)'
                r'(?:(?!也|还|请|帮|并|同时)[\w\u4e00-\u9fff]){1,30}')
_EXCLUSIVE_RECORD = r'(?:只|仅)(?:按|读|看|用)(?:本店|店铺|当前|本轮)?(?:已登记|已记录)?(?:记录|资料|参数)'
_SPEC_LIMIT = r'(?:不要|不用|无需)列(?:出)?(?:其他|其它)规格'
_UNRECORDED_CONSTRAINT = (
    r'(?:未知的|未记录的|未登记的)(?P<prefix>[\w\u4e00-\u9fff°%µΩ· /()（）\-]{2,30}?)'
    r'(?:不要|不用|无需|别|不)(?:猜(?:测)?|推测|推断)|'
    r'(?:^|(?<=[，,、]))\s*(?P<suffix>[\w\u4e00-\u9fff°%µΩ· /()（）\-]{2,30}?)'
    r'(?:无记录|未记录|没有记录|未登记|未知)(?:不要|不用|无需|别|不)(?:猜(?:测)?|推测|推断)'
)
_DIMENSION_FIELDS = {'尺寸', '外形尺寸', '机身尺寸', '长宽高'}
_USAGE_FIELDS = {'使用方式', '使用方法', '操作方式', '操作方法'}
_USAGE_KEYS = r'使用|操作|功能|档位|摇头|供电|续航|安装|按键|按钮|开关|清洁|护理|保养|注意|安全|适用'
_REFERENCE_SUFFIX = r'(?:性能|情况|能力|信息)$'
_VOLUME = r'(?<![A-Za-z0-9.\-])\d+(?:\.\d+)?\s*(?:mL|ml|毫升|L|升)(?![A-Za-z])'
_GENERIC_UNKNOWN = r'^(?:未知|未记录|未登记|未明确|未记载)(?:项|部分|内容|信息|参数|字段|事项)$'
_FIELD_PREFIX = (
    r'^(?:\s|[，,、：:“”「」『』"\']|与|和|及|以及|的|请|麻烦|帮我|'
    r'只|按|从|本店|店铺|商品|产品|本轮|目前|当前|现在|分别|各自|全部|所有|完整|逐项|各|每个|各个|各种|'
    r'再说|两款|两者|这两款|这两者|它们|这一规格|该规格|指定规格|'
    r'查询|查一下|查看|查|问|看看|列出|列一下|介绍|说明|说说|说|告诉我|对比|比较|'
    r'已记录的|已登记的|明确记录的|明确登记的|未知的|已知的|已记录|已登记|未知|已知|'
    r'记录上的|记录上|已知记录|记录参数|规格参数|参数记录|'
    r'指定字段|字段|记录|登记|资料|参数|标明的|标明)+'
)
_FIELD_SUFFIX = (
    r'(?:\s|[，,、：:“”「」『』"\']|的|吗|呢|是什么|是多少|分别|各自|列出|'
    r'分别是什么|分别是多少|分别有哪些|分别列出|各自是什么|各自是多少|也列出|也请列出|'
    r'有哪些|有什么|多少|什么|有无|是否记录|有没有记录|'
    r'记录上的差别|记录上的区别|记录上的差异|记录差别|记录区别|记录差异|'
    r'差别|区别|差异|不同|对比|比较|记录参数|规格参数|参数记录|'
    r'指定字段|字段|记录|登记|资料|参数|已知记录)+$'
)


def _without_record_constraints(clause):
    """Consume only instructions that narrow a record query, never other work."""
    clause = re.sub(_UNRECORDED_CONSTRAINT, '', clause)
    clause = re.sub(_NO_INFERENCE, '', clause)
    clause = re.sub(_NO_TRANSFER, '', clause)
    return re.sub(_EXCLUSIVE_RECORD, '', clause)


def _constraint_fields(clause):
    names = []
    for match in re.finditer(_UNRECORDED_CONSTRAINT, clause):
        target = (match.group('prefix') or match.group('suffix')).strip()
        if target and not re.search(_OTHER_WORK, target):
            names.append(target)
    for match in re.finditer(_NO_INFERENCE, clause):
        target = re.split(r'猜(?:测)?|推测|推断|外推', match[0], maxsplit=1)[-1].strip()
        target = re.sub(r'^(?:未知的|未记录的|未知|未记录)', '', target)
        if (re.fullmatch(r'[\w\u4e00-\u9fff°%µΩ· /()（）\-]{2,30}', target)
                and not re.search(r'我|你|您|很|太|请|帮|也|还|再|能否|是否|支持|不支持', target)):
            if target not in names:
                names.append(target)
    return names


def _declared_unknown_fields(clause):
    # Customer wording identifies the requested unknown; the renderer still
    # checks every source first and can replace that assumption with a value.
    return list(dict.fromkeys((m.group('prefix') or m.group('suffix')).strip()
                             for m in re.finditer(_UNRECORDED_CONSTRAINT, clause)))


def _record_clause(products, clause, mentions, explicit_scope=False, previous_products=(), qualified_parts=False):
    """Recognize noun fields and record comparisons without interpreting them.

    Field names are taken from the catalogue or the customer's explicit record
    question. Unknown names remain requests for an unknown field, not a guessed
    capability. Any action, judgment or unexplained sentence stays with the
    full agent. No product/model/function vocabulary is embedded here.
    """
    # Check before stripping constraints: "不要猜也请查订单" must never
    # lose its independent action inside the negated inference span.
    owners = [p for _, _, p in mentions] or list(previous_products)
    for constraint in re.finditer(_NO_INFERENCE, clause):
        if re.match(r'^不(?:猜|推测|推断|外推)', constraint[0]):
            target = re.split(r'猜(?:测)?|推测|推断|外推', constraint[0], maxsplit=1)[-1].strip()
            # The new short negation supports an explicit target such as
            # "不推测保温". Generic "不推断未知项" must not newly take over
            # an ordinary functional explanation and its proven supplements.
            if re.fullmatch(_GENERIC_UNKNOWN, target) or not _constraint_fields(constraint[0]):
                return None
    # An explicit single-SKU record question can narrow its output without
    # becoming a negated independent action. No inferred/past SKU is used.
    scoped_specs = bool(mentions and re.search(r'这一规格|该规格|指定规格', clause)
                        and any(str(s.get(k)) in clause for p in owners for s in p.get('skus', [])
                                for k in ('id', 'label', 'color', 'size') if s.get(k)))
    if not scoped_specs and re.search(r'这一规格|该规格|指定规格', clause):
        scoped_specs = bool(mentions and re.search(publication_state.SkuScope.CAPACITY, clause, re.I))
    checked = re.sub(_SPEC_LIMIT, '', clause) if scoped_specs else clause
    if re.search(_OTHER_WORK, checked):
        return None
    query = _without_record_constraints(checked)
    # Availability, variants and care already have a dedicated record grammar
    # and SKU projection. Do not reinterpret them as arbitrary spec labels.
    if re.search(r'库存|有货|缺货|现货|无货|还剩|补货|颜色|色号|尺码|包装|清单|配件|注意事项|洗护|护理|保养|清洁', query):
        return None
    comparison = bool(re.search(_COMPARISON, query))
    explicit = bool(re.search(_RECORD_MARKER, query))
    explicit_fields = '字段' in query
    original_strict = bool(explicit_scope or scoped_specs or re.search(_STRICT_RECORD, query))
    added_strict = bool(not comparison and len({p['id'] for p in owners}) == 1
                        and re.search(_EXPLICIT_RECORD_FIELD, query))
    strict = original_strict or added_strict
    if comparison and len({p['id'] for p in owners}) < 2:
        return None
    remainder = clause
    # Constraint removal occurs after identities, so coordinates stay valid.
    for start, end, _ in reversed(mentions):
        remainder = remainder[:start] + ' ' + remainder[end:]
    if scoped_specs:
        remainder = re.sub(_SPEC_LIMIT, '', remainder)
    constraints = _constraint_fields(remainder)
    declared = _declared_unknown_fields(remainder)
    remainder = _without_record_constraints(remainder)
    remainder = re.sub(r'(?:我|我们)(?:都)?想了解\s*[，,]', '', remainder)
    for _, _, product in mentions:
        if not scoped_specs and any(str(s.get(k)) in remainder for s in product.get('skus', [])
               for k in ('id', 'color', 'size') if s.get(k)):
            return None
    if scoped_specs:
        for p in owners:
            for sku in p.get('skus', []):
                for key in ('id', 'label', 'color', 'size'):
                    if sku.get(key):
                        remainder = re.sub(re.escape(str(sku[key])), ' ', remainder, flags=re.I)
        remainder = re.sub(publication_state.SkuScope.CAPACITY, ' ', remainder, flags=re.I)
    elif re.search(publication_state.SkuScope.COLORS + '|' + publication_state.SkuScope.SIZE
                 + '|' + publication_state.SkuScope.CAPACITY, remainder, re.I):
        return None
    available = {str(k) for p in owners for k in _specs(p)}
    available.update(str(k) for p in owners for s in p.get('skus', []) for k in _specs(s))
    if any(_sku_records(p, '容量', p.get('skus', [])) for p in owners):
        available.add('容量')
    protected = {}
    def protect(match):
        key = '§' + str(len(protected)) + '§'
        protected[key] = match[0]
        return key
    # A custom field can itself contain query words, punctuation or "和".
    # Protect current catalogue keys before consuming grammar around them.
    if available:
        remainder = re.sub('|'.join(re.escape(k) for k in sorted(available, key=len, reverse=True) if k),
                           protect, remainder)
    remainder = re.sub(_FIELD_PREFIX, '', remainder)
    remainder = re.sub(_FIELD_SUFFIX, '', remainder)
    remainder = remainder.strip(' \t，,、：:')
    if not remainder:
        # Existing ordinary specs queries own their stock/price/package
        # projection. Only an explicit comparison or field enumeration uses
        # this narrower spec-field publisher.
        if not explicit or not mentions or (not comparison and not explicit_fields):
            return None
        return {'record_fields': None, 'comparison': comparison,
                'constraint_fields': constraints,
                **({'declared_unknown_fields': declared} if declared else {})}
    names = []
    for part in re.split(r'[，,、；;]+|以及|和|与|及', remainder):
        name = re.sub(_FIELD_PREFIX, '', part.strip())
        name = re.sub(_FIELD_SUFFIX, '', name).strip(' \t“”「」『』"\'')
        for key, label in protected.items():
            name = name.replace(key, label)
        if not name:
            continue
        # Noun lists are deliberately bounded. Verbs, nested requests and
        # complete unexplained clauses cannot become a made-up field label.
        if (not re.fullmatch(r'[\w\u4e00-\u9fff°%µΩ· /()（）\-]{1,40}', name)
                or (len(name) == 1 and name not in available)
                or re.search(r'请|帮|需要|想|要|能否|是否|能不能|是不是|支持|不支持|'
                             r'别|不要|不用|不用查|告诉|解释|处理|申请|撤销|取消|也|再|'
                             r'用来|用于|可以|不能|能用|帮忙|顺手|其他|其它|我|你|您|很|太|'
                             r'有什么|哪些|几个|同口径|实测|扩大|场景', name)):
            return None
        if name not in names:
            names.append(name)
    # A bare noun outside the current catalogue is not enough to identify a
    # record query. Explicit "字段/记录/参数" permits unknown custom fields.
    known_names = {name for name in names if name in available or (strict and name in _DIMENSION_FIELDS | _USAGE_FIELDS) or any(
        _related_records(_specs(p), name) for p in owners)}
    part_names = {name for name in names if qualified_parts and any(
        len(key) >= 2 and name.endswith(key) and 2 <= len(name.removesuffix(key)) <= 16
        for key in available)}
    if not names or (not explicit_fields and not scoped_specs and any(name not in known_names | part_names for name in names)):
        return None
    if (added_strict and not original_strict and len(names) == 1
            and names[0] not in _DIMENSION_FIELDS | _USAGE_FIELDS
            and not re.search(r'已记录|已登记|明确记录|明确登记|标明', query)
            and not any(_sku_records(p, names[0], p.get('skus', [])) for p in owners)):
        # A broad single-feature "资料是什么" explanation had a writer path
        # that preserves other verified facts. Do not redefine it as exactly
        # one database key. Literal fields and the r4 size/variant gaps above
        # remain fully bounded record requests.
        return None
    paired = len({p['id'] for p in owners}) >= 2 and bool(re.search(r'分别|各自|两款|两者', query))
    bounded_pair = paired and bool(_constraint_fields(clause) or ('记录' in query and all(name in available for name in names)))
    if not strict and not qualified_parts and not bounded_pair and not (comparison and _constraint_fields(clause)):
        return None
    return {'record_fields': names, 'comparison': comparison or bounded_pair,
            'constraint_fields': constraints,
            **({'declared_unknown_fields': declared} if declared else {})}


def plan(ctx, question, product_mentions):
    products = ctx.get('products', [])
    requests, complete, previous = [], True, []
    original_scope = bool(re.search(r'只(?:按|读|看)(?:本店|店铺)?(?:记录|资料)', question))
    single_owner = len({p['id'] for _, _, p in product_mentions(products, question)}) == 1
    explicit_scope = original_scope or (single_owner and bool(re.search(_EXCLUSIVE_RECORD, question)))
    qualified_parts = bool(re.search(_NO_TRANSFER, question))
    fragments = re.split(r'[。；;！？?\n]+|另外|同时|顺便|并且|另(?=问|说|查|看|列)', question)
    for clause in fragments:
        if not clause.strip():
            continue
        if (not re.search(_OTHER_WORK, clause)
                and not _without_record_constraints(clause).strip(' \t，,、')):
            if previous:
                for prior in previous:
                    prior['constraint_fields'] = list(dict.fromkeys(
                        [*prior.get('constraint_fields', []), *_constraint_fields(clause)]))
                    if declared := _declared_unknown_fields(clause):
                        prior['declared_unknown_fields'] = list(dict.fromkeys(
                            [*prior.get('declared_unknown_fields', []), *declared]))
            continue
        fields = []
        if re.search(r'库存|有货|缺货|现货|无货|还剩', clause): fields.append('inventory')
        if '补货' in clause: fields.append('restock')
        if re.search(r'规格参数|参数(?:有|是|包括|包含|如下)|(?:查|看|介绍|列出|说明).{0,8}参数', clause): fields.append('specs')
        if re.search(r'颜色|色号|尺码', clause): fields.append('variants')
        if re.search(r'包装|清单|配件', clause): fields.append('package')
        if re.search(r'注意事项|洗护|护理|保养|清洁', clause): fields.append('care')
        mentions = product_mentions(products, clause)
        unknown = [m[0] for m in re.finditer(r'(?<![A-Za-z0-9])[A-Za-z]{1,3}\d+(?:\s*Pro)?(?![A-Za-z0-9])', clause)
                   if not any(a <= m.start() and m.end() <= b for a, b, _ in mentions)
                   and not product_mentions(products, m[0])]
        previous_products = [p for p in products if p['id'] in {r.get('product_id') for r in previous}
                             and any('record_fields' in r for r in previous)]
        inherited_scope = bool(previous and any('record_fields' in r for r in previous))
        record = _record_clause(products, clause, mentions, explicit_scope or inherited_scope,
                                previous_products, qualified_parts) if not unknown else None
        if record:
            fields = ['record_fields']
        if not record and (not fields or re.search(
                r'订单|售后|退货|退款|换货|赔|物流|收货|签收|发票|人工|'
                r'如果|假如|假设|上次|曾经|历史|之前|以前|昨天|明天|以后|未来|保证|'
                r'推荐|比较|对比|区别|适合|合身|怎么选|如何选|尺码表|购买|下单|报价|实付|优惠|价格|多少钱|'
                r'不要|不用|无需|别(?:查|看|列|说)', clause)):
            complete = False
            previous = []
            continue
        # Consume only catalogue identities and a small record-query grammar.
        # Unknown words are independent work, not permission to drop a topic.
        remainder = '' if record else clause
        if not record:
            for start, end, _ in reversed(mentions):
                remainder = remainder[:start] + ' ' + remainder[end:]
        for word in unknown: remainder = remainder.replace(word, ' ')
        for product in products:
            for sku in product.get('skus', []):
                for value in (sku.get('id'), sku.get('color'), sku.get('size')):
                    if value:
                        remainder = re.sub(re.escape(str(value)), ' ', remainder, flags=re.I)
        tokens = (r'规格参数|规格|参数|颜色|色号|尺码|库存|有货|缺货|现货|无货|补货|包装|清单|配件|注意事项|洗护|护理|保养|清洁|'
                  r'暖白|白色|黑色|曜石黑|绿色|米色|燕麦色|燕麦|鼠尾草绿|'
                  r'查询|查一下|查|看看|看|告诉我|介绍|列出|列一下|说明|帮我|麻烦|请|只|'
                  r'目前|当前|现在|分别|各自|全部|所有|每个|各个|各种|各|完整|逐项|逐个|'
                  r'有哪些|哪(?:些|种|个)|什么|是否|有无|有没有|多少|如何|怎样|怎么样|情况|'
                  r'时间|日期|记录|登记|包含|包括|还有|还剩|有|是|的|和|与|及|或|吗|呢|码')
        remainder = re.sub(tokens, '', remainder)
        if re.sub(r'[\s，,、：:/()（）\-]', '', remainder):
            complete = False
            previous = []
            continue
        if unknown:
            requests.extend({'unknown_model': name, 'fields': fields, 'clause': clause} for name in unknown)
            previous = []
            if not mentions: continue
        if not mentions:
            if previous:
                requests.extend({**prior, **(record or {}), 'fields': fields, 'clause': clause} for prior in previous)
            else:
                complete = False
            continue
        unique = list({p['id']: p for _, _, p in mentions}.values())
        shared_tail = ''
        if len(mentions) > 1 and all(re.fullmatch(r'\s*(?:和|与|及|、)\s*', clause[a[1]:b[0]])
                                     for a, b in zip(mentions, mentions[1:])):
            shared_tail = clause[mentions[-1][1]:]
        previous = []
        for product in unique:
            positions = [(a, b) for a, b, p in mentions if p['id'] == product['id']]
            first = positions[0][0]
            following = next((a for a, _, p in mentions if a > first and p['id'] != product['id']), len(clause))
            subject = clause[:following] if first == mentions[0][0] else clause[first:following]
            if shared_tail: subject += shared_tail
            scope = publication_state.SkuScope([product], product_mentions, None)
            scope.consume(subject)
            selected = scope.candidates()
            if not selected and not record:
                complete = False
                continue  # Unknown/ambiguous specifications remain the agent's work.
            request = {'product_id': product['id'], 'sku_ids': [s['id'] for s in selected],
                       'dimensions': deepcopy(scope.dimensions),
                       'paired_skus': [s['id'] for s in scope.pairs] if scope.pairs is not None else None,
                       'fields': fields, 'clause': clause}
            if record:
                request.update(record)
            requests.append(request)
            previous.append(request)
    return {'requests': requests, 'complete': complete,
            'comparison': any(r.get('comparison') for r in requests)} if requests else None


def _specs(product):
    value = product.get('specs')
    return value if isinstance(value, dict) else {}


def _recorded(value):
    return value is not None and value != '' and value != [] and value != {}


def _record_value(value):
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _sku_records(product, name, skus):
    """Quote a selected variant field; never turn model digits into a value."""
    records = []
    for sku in skus:
        value = _specs(sku).get(name)
        if _recorded(value) and _record_value(value):
            records.append({'sku_id': sku['id'], 'source_field': 'specs.' + name,
                            'value': deepcopy(value), 'original': deepcopy(value)})
            continue
        if name != '容量':
            continue
        # The volume unit in the actual variant record establishes what the
        # number describes. SKU identifiers and prices are never capacities.
        for field in ('label', 'color', 'size'):
            original = sku.get(field)
            if not isinstance(original, str):
                continue
            if re.search(_VOLUME, original, re.I):
                # Keep ranges, approximations and alternative fill markings.
                # Extracting just a number would change what was recorded.
                records.append({'sku_id': sku['id'], 'source_field': field,
                                'value': original, 'original': original})
                break
    return records


def _related_records(specs, name):
    related = []
    reference_name = re.sub(_REFERENCE_SUFFIX, '', name)
    for key, value in specs.items():
        key = str(key)
        if key == name or not _recorded(value):
            continue
        raw = _record_value(value)
        # A qualified field (e.g. a component + material) may be supported by
        # that qualifier in the same product's base-field text. Always quote
        # the original base field; never relabel generic material as a part.
        qualifier = (name.removesuffix(key) if name.endswith(key) else
                     name.removeprefix(key) if name.startswith(key) else '')
        if (name in raw or (len(key) >= 2 and len(qualifier) >= 2 and qualifier in raw)
                or (reference_name != name and len(reference_name) >= 2
                    and (reference_name == key or reference_name in raw))):
            related.append((key, value))
    return related


def _record_lines(product, names, evidence, skus=(), declared_unknown_fields=()):
    specs = _specs(product)
    if not names:
        return ['规格参数尚未登记，不能据其他商品推断']
    lines = []
    for name in names:
        value = specs.get(name)
        present = name in specs and _recorded(value) and _record_value(value).strip()
        evidence.append({'product_id': product['id'], 'field': name,
                         'recorded': bool(present), 'value': deepcopy(value) if present else None})
        if present:
            lines.append(name + '：' + _record_value(value))
        sku_records = _sku_records(product, name, skus)
        if sku_records:
            by_sku = {sku['id']: sku for sku in skus}
            def variant_line(row):
                line = row['sku_id'] + '：' + _record_value(row['value'])
                if name == '容量':
                    stock = by_sku[row['sku_id']].get('stock')
                    line += ('（当前缺货，库存0件）' if stock == 0 and type(stock) is int
                             else '（当前有货，库存' + str(stock) + '件）' if type(stock) is int and stock > 0
                             else '（库存尚未核实）')
                    row['stock'] = stock if type(stock) is int and stock >= 0 else None
                return line
            lines.append(name + '按当前查询规格的原文：' + '；'.join(
                variant_line(row) for row in sku_records))
            evidence[-1]['recorded'] = True
            evidence[-1]['sku_records'] = sku_records
            # Variant capacity is accompanied by that product's actual
            # description/care records. Valid safety/care information from
            # the former answer must survive rebuilding the complete range.
            if name == '容量' and not present:
                references = [(str(key), value) for key, value in specs.items()
                              if _recorded(value) and _record_value(value)]
                if references:
                    lines.append('本商品附带已登记字段原文：' + '；'.join(
                        key + '：' + _record_value(value) for key, value in references))
                    evidence[-1]['reference_records'] = [{'field': key, 'value': deepcopy(value)} for key, value in references]
        if present or sku_records:
            continue
        if name in _USAGE_FIELDS:
            related = [(str(key), value) for key, value in specs.items()
                       if re.search(_USAGE_KEYS, str(key)) and _recorded(value) and _record_value(value)]
            if related:
                lines.append(name + '相关字段原文：' + '；'.join(
                    key + '：' + _record_value(value) for key, value in related))
                evidence[-1]['recorded'] = True
                evidence[-1]['related_records'] = [{'field': key, 'value': deepcopy(value)} for key, value in related]
                continue
        if name in _DIMENSION_FIELDS:
            related = [(str(key), value) for key, value in specs.items()
                       if key in _DIMENSION_FIELDS and _recorded(value) and _record_value(value)]
            if related:
                lines.append(name + '相关字段原文：' + '；'.join(
                    key + '：' + _record_value(value) for key, value in related))
                evidence[-1]['recorded'] = True
                evidence[-1]['related_records'] = [{'field': key, 'value': deepcopy(value)} for key, value in related]
                continue
        # A function can be recorded inside another field. Quote that exact
        # same-product field without turning a lexical match into a verdict.
        related = _related_records(specs, name)
        if related:
            label = (name + '相关字段原文：' if re.search(_REFERENCE_SUFFIX, name)
                     else '字段“' + name + '”未单独登记；本商品相关字段原文：')
            lines.append(label + '；'.join(key + '：' + _record_value(value) for key, value in related))
            if re.search(_REFERENCE_SUFFIX, name):
                lines.append('以上仅引用已登记原文，不据此推测未登记的实际表现')
            evidence[-1]['related_records'] = [{'field': key, 'value': deepcopy(value)} for key, value in related]
        else:
            line = (name + '未记载，不能确认，不据其他商品推断' if name in declared_unknown_fields
                    else '字段“' + name + '”本商品当前未记录，不能确认，不据其他商品推断')
            base = [(str(key), value) for key, value in specs.items() if _recorded(value)
                    and len(str(key)) >= 2 and name.endswith(str(key))
                    and len(name.removesuffix(str(key))) >= 2]
            if base:
                line += '；本商品已登记的原字段为' + '；'.join(key + '：' + _record_value(value) for key, value in base)
                line += '，该原文不能据此认定所问部件'
                evidence[-1]['reference_records'] = [{'field': key, 'value': deepcopy(value)} for key, value in base]
            lines.append(line)
    return lines


def render(plan, ctx, facts, normalized_price_quote=None):
    if not plan or ctx.get('catalog_evidence', {}).get('source') != 'fresh_database':
        return None
    products = deepcopy(ctx.get('products', []))
    by_id = {p['id']: p for p in products}
    records = {s['id']: s for p in products for s in p.get('skus', [])}
    quotes = []
    for fact in facts or []:
        value = fact.get('result')
        tool = fact.get('tool')
        if not isinstance(value, dict) or value.get('error'):
            continue
        if tool == 'get_product' and value.get('id') in by_id:
            # get_product is a complete fresh product observation. Do not fill
            # its missing/unknown fields with the earlier catalogue snapshot.
            product = deepcopy(value)
            by_id[product['id']] = product
            old_ids = {s['id'] for s in records.values() if s.get('product_id') == product['id']}
            old_ids.update(s['id'] for p in products if p['id'] == product['id'] for s in p.get('skus', []))
            for sid in old_ids: records.pop(sid, None)
            records.update((s['id'], s) for s in product.get('skus', []))
            continue
        if tool not in ('get_inventory', 'quote_price'): continue
        sid = value.get('sku_id') or value.get('id')
        if sid not in records:
            continue
        records[sid].update({k: value[k] for k in ('stock', 'restock_at') if k in value})
        if tool == 'get_inventory':
            for key in ('stock', 'restock_at'):
                if key not in value: records[sid].pop(key, None)
        elif normalized_price_quote and (quote := normalized_price_quote(value)):
            records[sid]['price_cents'] = quote['unit_price_cents']
            # A quote observed after a same-turn revision owns both its unit
            # price and version. Retaining the earlier catalogue version would
            # wrongly reject this fresh quote at the publication boundary.
            if type(quote.get('sku_version')) is int:
                records[sid]['version'] = quote['sku_version']
            quotes.append(quote)
    sections, displayed_products, displayed_quotes, emitted = [], [], [], set()
    record_evidence = []
    for request in plan['requests']:
        if request.get('unknown_model'):
            sections.append(f"本轮商品记录中未找到 {request['unknown_model']}，请确认商品名称或型号，无法据其他商品判断它的库存或参数。")
            continue
        product = by_id.get(request['product_id'])
        if not product: continue
        model = product.get('model') or product['id']
        if 'dimensions' in request and request.get('paired_skus') is None:
            # Reapply only this question's scope to the latest complete record.
            scope = publication_state.SkuScope([product], lambda *_: [], None)
            scope.owner, scope.dimensions = product, request['dimensions']
            selected = scope.candidates()
        else:
            selected = [records[sid] for sid in request['sku_ids'] if sid in records]
        if 'record_fields' in request:
            if request.get('dimensions') and not selected:
                sections.append(model + ' 的最新记录未能核实所查规格，不能据其他规格回答所查字段。')
                continue
            names = request['record_fields']
            if names is None:
                peers = [by_id.get(r.get('product_id')) for r in plan['requests']
                         if r.get('clause') == request.get('clause') and 'record_fields' in r]
                names = list(dict.fromkeys(str(key) for p in peers if p
                             for key in _specs(p)))
            names = list(dict.fromkeys([*names, *request.get('constraint_fields', [])]))
            lines = _record_lines(product, names, record_evidence, selected,
                                  request.get('declared_unknown_fields', []))
            section = '### ' + model + '\n本商品本轮记录：\n' + '\n'.join('- ' + line for line in lines)
            if section not in emitted:
                emitted.add(section)
                sections.append(section)
            if product not in displayed_products:
                displayed_products.append(product)
            continue
        if not selected:
            sections.append(model + ' 的最新记录未能核实所查规格，请确认具体规格。')
            continue
        fields = set(request['fields'])
        if product not in displayed_products: displayed_products.append(product)
        lines = []
        if 'variants' in fields and '尺码' in request['clause'] and not any(s.get('size') for s in selected):
            lines.append('没有尺码区分，已登记的规格如下')
        if 'specs' in fields:
            specifications = [str(key) + '：' + str(value) for key, value in (product.get('specs') or {}).items()
                              if value is not None and str(value).strip()]
            lines.extend(specifications or ['规格参数尚未登记'])
            if product.get('package_contents'):
                lines.append('包装：' + '、'.join(map(str, product['package_contents'])))
        elif 'package' in fields:
            lines.append('包装：' + '、'.join(map(str, product['package_contents']))
                         if product.get('package_contents') else '包装清单尚未登记')
        if 'care' in fields:
            care = [str(key) + '：' + str(value) for key, value in (product.get('specs') or {}).items()
                    if re.search(r'注意|洗护|护理|保养|清洁|防护|安全', str(key)) and value is not None]
            lines.extend(care or ['注意及护理事项尚未登记，不据其他参数推断'])
        if fields.intersection({'inventory', 'restock'}):
            known = all(type(s.get('stock')) is int and s['stock'] >= 0 for s in selected)
            if known and all(s['stock'] > 0 for s in selected):
                lines.append('查询范围内未见缺货规格')
            elif not known:
                lines.append('部分库存尚未核实，不能给出完整缺货结论')
        for sku in selected:
            if not fields.intersection({'inventory', 'restock', 'variants', 'specs'}): continue
            details = []
            stock = sku.get('stock')
            if fields.intersection({'inventory', 'restock', 'variants', 'specs'}):
                details.append(('查询时有货，库存 ' + str(stock) + ' 件' if stock > 0 else '当前缺货，库存 0 件')
                               if type(stock) is int and stock >= 0 else '库存尚未核实，不能判断是否缺货')
            if 'restock' in fields or stock == 0:
                date = catalog_advice._beijing_restock_date(sku.get('restock_at'))
                details.append('预计补货日期为 ' + date + '，预计时间不保证到货' if date else
                               '补货日期未记录（尚未登记）' if 'restock_at' in sku and sku['restock_at'] in (None, '')
                               else '补货日期尚未核实')
            if fields.intersection({'variants', 'specs'}) and type(sku.get('price_cents')) is int:
                details.append(f"当前单价{yuan(sku['price_cents'])}元")
            candidates = [q for q in quotes if q['sku_id'] == sku['id'] and q['quantity'] == 1
                          and q['unit_price_cents'] == sku.get('price_cents')]
            values = {(q['unit_price_cents'], q['discount_cents'], q['total_cents']) for q in candidates}
            if len(values) == 1:
                quote = deepcopy(candidates[-1])
                # A quote retains its audited amount, but its display uses the
                # later availability observation. Never mutate the tool record.
                quote.update(stock=stock, restock_at=sku.get('restock_at'))
                quote['available'] = type(stock) is int and stock >= quote['quantity']
                quote['purchase_allowed'] = quote['available']
                if not quote['available']:
                    quote['unavailable_reason'] = '库存尚未核实' if type(stock) is not int else '当前库存不足'
                details.append(f"按1件核价，优惠{yuan(quote['discount_cents'])}元，实付{yuan(quote['total_cents'])}元")
                if quote not in displayed_quotes: displayed_quotes.append(quote)
            label = ' '.join(str(v) for v in (sku.get('color'), sku.get('size')) if v) or sku.get('label') or sku['id']
            if stock == 0 and 'restock' in fields and 'restock_at' in sku and sku['restock_at'] in (None, ''):
                lines.append('当前缺货规格中，' + model + label + '没有记录补货日期。')
            lines.append(model + ' ' + label + '：' + '；'.join(details))
        section = '### ' + model + '\n本轮记录：\n' + '\n'.join('- ' + line for line in lines)
        if section not in emitted:
            emitted.add(section)
            sections.append(section)
    if not sections: return None
    if plan.get('comparison'):
        sections.insert(0, '以下按各商品本轮记录逐项对照；未记录不代表不支持，不据参数推测实际表现。')
    # Package-only prose need not display prices, but its product card must
    # retain the current quote evidence already observed in this turn. Pass
    # only identifiers: the business boundary rechecks owner, time, quantity,
    # catalogue version and current promotion before using any stored amount.
    displayed_skus = {s['id']: s for p in displayed_products for s in p.get('skus', [])}
    verification_quote_ids = list(dict.fromkeys(q['quote_id'] for q in quotes
        if q.get('quote_id') and q['quantity'] == 1 and q['sku_id'] in displayed_skus
        and q['unit_price_cents'] == displayed_skus[q['sku_id']].get('price_cents')))
    return {'text': '\n\n'.join(sections), 'products': displayed_products, 'quotes': displayed_quotes,
            'verification_quote_ids': verification_quote_ids,
            'evidence': {'source': 'fresh_database_and_current_tools', 'requests': deepcopy(plan['requests']),
                         'records': record_evidence}}


def replace_covered_record_prose(text, plan, rendered, product_mentions, catalogue_products=()):
    """Let the shared renderer own covered records inside a mixed reply.

    This returns the unowned prose for the caller to join with rendered.text.
    It removes complete record clauses, rather than appending corrections next
    to conflicting claims. Independent service/shopping clauses and records
    about other products stay with the full agent. No raw messages or tool
    observations are modified.
    """
    requests = [r for r in (plan or {}).get('requests', []) if 'record_fields' in r]
    if (not requests or not rendered or
            rendered.get('evidence', {}).get('source') != 'fresh_database_and_current_tools'):
        return text
    products = list({p['id']: p for p in [*catalogue_products, *rendered.get('products', [])]}.values())
    covered = {r['product_id'] for r in requests}
    by_id = {p['id']: p for p in products}
    field_names = set()
    value_fragments = set()
    for request in requests:
        product = by_id.get(request['product_id'], {})
        fields = request.get('record_fields')
        fields = list(_specs(product)) if fields is None else list(fields)
        fields += request.get('constraint_fields', [])
        field_names.update(str(name) for name in fields)
        for name in fields:
            value = _specs(product).get(name)
            values = [value] if _recorded(value) else [v for _, v in _related_records(_specs(product), name)]
            if name in _USAGE_FIELDS:
                usage = [(str(key), value) for key, value in _specs(product).items()
                         if re.search(_USAGE_KEYS, str(key)) and _recorded(value)]
                field_names.update(key for key, _ in usage)
                values += [value for _, value in usage]
            if name in _DIMENSION_FIELDS and not any(_recorded(value) and _record_value(value)
                    for key, value in _specs(product).items() if key in _DIMENSION_FIELDS):
                # These assertions answer physical size without a source. A
                # generic capacity or usage record cannot substantiate them.
                field_names.update({'不占地', '占地很小', '占地小', '桌面放得下',
                                    '台面放得下', '桌面能放下', '台面能放下', '摆得下'})
            for record in _sku_records(product, name, product.get('skus', [])):
                values.append(record['value'])
            for value in values:
                value_fragments.update(part.strip() for part in re.split(r'[，,、；;\n]+', _record_value(value))
                                       if len(part.strip()) >= 2)
    vocabulary = sorted(field_names | value_fragments, key=len, reverse=True)
    record_pattern = '|'.join(r'\s*'.join(re.escape(part) for part in term.split()) for term in vocabulary if term)
    independent = (r'订单|售后|退货|退款|换货|物流|收货|签收|发票|开票|人工|'
                   r'推荐|选购|购买|下单|报价|实付|优惠|价格|预算|付款|支付|保修|质保|'
                   r'下载|上传|删除|修改|撤销|取消')
    comparison_context = bool(len(covered) == 1 or (plan.get('comparison') and len(covered) >= 2))
    last_owners = set()
    kept = []
    chunks = re.findall(r'[^。！？!?；;，,\n]*(?:[。！？!?；;，,\n]+|$)', text)
    pieces = [piece for chunk in chunks for piece in re.split(r'(?=但是|但|不过|同时|而且)', chunk)]
    for piece in pieces:
        if not piece:
            continue
        named = product_mentions(products, piece)
        owners = {p['id'] for _, _, p in named}
        # Resolve against the whole catalogue as well as current projections,
        # so another product's parameter answer is never owned by this query.
        if owners:
            last_owners = owners
            comparison_context = bool(owners.issubset(covered))
        elif (plan.get('comparison') and len(covered) >= 2
                and (not last_owners or last_owners.issubset(covered))
                and re.search(r'两者|两款|两种|共同|均|都', piece)):
            # A preceding invoice/status clause does not rebind "both" to
            # that service topic. An explicit third product still does.
            comparison_context = True
        record_term = re.search(record_pattern, piece) if record_pattern else None
        business = re.search(independent, piece)
        if business:
            # A comma-less record assertion can precede a service clause.
            # Preserve the exact business suffix, including its uncertainty.
            if (comparison_context and record_term and record_term.start() < business.start()
                    and business.start() > 0):
                kept.append(piece[business.start():])
            else:
                kept.append(piece)
            comparison_context = False
            continue
        record_heading = re.search(r'记录|参数|字段|相同|不同|差别|差异|共同', piece)
        record_assertion = re.search(r'支持|具有|具备|配备|拥有|能够|还有|两款|两者|两种|都|均', piece)
        owns_piece = comparison_context and (record_term or record_heading or record_assertion)
        if not owns_piece:
            kept.append(piece)
    return ''.join(kept).strip()
