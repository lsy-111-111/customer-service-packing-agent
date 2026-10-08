"""Typed customer statements. Order dates, money and policy never come from a model."""
import re
from .delivery_responsibility import protect_delivery_responsibility

ENUMS = {
    'issue_type': {'no_reason', 'quality', 'damage', 'missing', 'wrong_item', 'unknown'},
    'condition': {'intact', 'inspection_only', 'used', 'damaged', 'unknown'},
    'responsibility': {'seller', 'customer', 'unknown'},
}
BOOLEANS = {'defect_on_arrival', 'accessories_complete', 'normal_use', 'repair_failed', 'mismatch_resolved', 'causation_conflict', 'unopened_return_basis'}
NUMBERS = {'missing_quantity', 'received_quantity'}
FIELDS = set(ENUMS) | BOOLEANS | NUMBERS | {'part_name'}


def unopened_return_basis_quote(text, *, order_id=None):
    """Find the latest complete, asserted unopened-return statement.

    This is a customer statement supporting advice, not proof that the item
    body was inspected and found intact. Callers scope chat text to one order
    before using it. A later contrary statement removes the earlier basis.
    """
    if not isinstance(text, str):
        return None
    unopened = r'(?:没(?:有)?|未|尚未|从未)(?:拆封|开封)'
    unused = r'(?:没(?:有)?|未|尚未|从未)(?:实际)?(?:使用过|使用|用过|用)'
    paired = (r'(?:(?:配件|附件)(?:和|及|与|、)?(?:包装|外盒|外箱)'
              r'|(?:包装|外盒|外箱)(?:和|及|与|、)?(?:配件|附件))'
              r'.{0,6}(?:齐全|完整|完好|都在)')
    separate = (r'(?:配件|附件).{0,4}(?:齐全|完整|都在)',
                r'(?:包装|外盒|外箱).{0,4}(?:齐全|完整|完好|无破损)')
    quality = r'(?:没有|没|无)(?:任何)?质量(?:问题|故障)|质量(?:没|没有)问题'
    disavow = (r'不是没(?:有)?(?:拆封|开封|用过|使用)|并非没(?:有)?(?:拆封|开封|用过|使用)'
               r'|(?:并不是|不是|并非)(?:没有|没|无)质量(?:问题|故障)'
               r'|(?:拆封|开封|用过|使用|齐全|完整|质量问题|质量故障)(?:吗|么|[？?])'
               r'|(?:并不|不是|并非|没有|没|未).{0,4}(?:说|表示|确认).{0,16}(?:拆封|开封|使用|用过)'
               r'|(?:撤回|说错|不对|不属实).{0,30}(?:拆封|开封|使用|用过|包装|配件|质量|完好)'
               r'|(?:之前|刚才|原先).{0,30}(?:拆封|开封|使用|用过|包装|配件|质量|完好).{0,8}(?:不对|说错|不属实|撤回)')
    adverse = (r'(?:已经|已|后来|现在|其实|实际(?:上)?).{0,8}(?<!没)(?<!未)(?<!有)(?:拆(?:过)?封|拆开|开(?:过)?封|使用(?:了|过)|用(?:了|过)|戴(?:了|过)|穿(?:了|过)|洗(?:了|过))'
               r'|(?:拆(?:了|过)?包装|包装(?:已|已经)?拆开|开箱(?:使用|用过))'
               r'|(?:本体|商品|产品|东西|耳机|这副|这件).{0,10}(?:损坏|破损|划痕|污损|坏了|开裂|裂了|不完好)'
               r'|(?:本体|商品|产品|东西|耳机|屏幕|这副|这件).{0,12}(?:裂|碎|摔坏|弄坏|有故障|出故障)'
               r'|(?:外壳|机身|表面).{0,8}(?:磕|裂|碎|破|污)'
               r'|(?:摔坏|砸坏|弄坏|裂了|碎了)'
               r'|(?:有|出现)(?:明显)?(?:划痕|污损|破损|损坏)'
               r'|(?<!没)(?:有|出现).{0,5}(?:故障|质量问题)'
               r'|(?<!没)(?<!无)有(?:杂音|异响)'
               r'|(?:出现|发生|发现|(?<!没)有).{0,5}质量(?:问题|故障)|质量.{0,4}(?:出了|出现|有)问题'
               r'|(?:包装|外盒|外箱).{0,8}(?:破损|不全|缺失|拆开)'
               r'|(?:配件|附件).{0,8}(?:不齐|不全|缺失|少了|少一|缺一|漏了)')
    adverse += r'|(?:充电盒|底座|充电线|配件|附件).{0,8}(?:丢了|遗失|少了|缺了|坏了)'
    adverse += r'|(?:少了|缺了|丢了).{0,8}(?:充电盒|底座|充电线|配件|附件)'
    adverse += r'|(?:我|耳机|这副|这件).{0,12}(?<!没)(?<!有)(?<!未)(?:用|戴|穿)(?:了|过)'
    uncertain = (r'(?:本体|商品|产品|东西|耳机|外观|状态|包装|配件|拆封|开封|使用|用过).{0,12}(?:不确定|不清楚|没检查|未检查|没确认|未确认|是否齐全|无法确认|不能确认)'
                 r'|(?:不确定|不清楚|没检查|未检查|没确认|未确认|无法确认|不能确认).{0,12}(?:本体|商品|产品|东西|耳机|外观|状态|包装|配件|拆封|开封|使用|用过)')
    retract = r'(?:刚才那句|上面说的|之前说的|上面那句).{0,20}(?:举例|示例|引用|不属实|不对|撤回|不是)|并不属实|不是我的订单情况'
    candidate = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # A direct form may paste statements about another order. Chat callers
        # already scope their text; forms must also bind an explicit order ID.
        if order_id and '订单' in line:
            mentioned = set(re.findall(r'订单\s*([A-Za-z0-9][A-Za-z0-9_-]{3,})', line))
            if (mentioned and mentioned != {order_id}) or (not mentioned and order_id not in line):
                continue
        blocked = bool(re.search(disavow, line) or re.search(adverse, line)
                       or re.search(uncertain, line) or re.search(retract, line))
        if blocked:
            candidate = None
            continue
        if re.search(r'如果|假如|假设|要是|倘若|朋友|别人|听说|(?:没有|没|未)(?:说|表示|确认)|(?:引用|示例|例子|举例)|[“”「」]', line):
            continue
        package_complete = bool(re.search(paired, line) or all(re.search(pattern, line) for pattern in separate))
        if all((re.search(unopened, line), re.search(unused, line), package_complete, re.search(quality, line))):
            candidate = line
    return candidate


def normalize(raw, source='customer'):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError('facts 必须是事实对象')
    result = {}
    for key, item in raw.items():
        if key not in FIELDS:
            continue
        wrapped = item if isinstance(item, dict) else {'value': item}
        value = wrapped.get('value')
        if wrapped.get('source', 'customer') not in ('customer', 'customer_statement', 'user', 'model_extraction'):
            raise ValueError('事实来源必须是顾客陈述，不能伪造人工核实或订单事实')
        if value is None:
            continue
        if key in ENUMS and (not isinstance(value,str) or value not in ENUMS[key]):
            raise ValueError(f'{key} 的事实值无效')
        if key in BOOLEANS and not isinstance(value, bool):
            raise ValueError(f'{key} 必须是布尔值')
        if key in NUMBERS and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 99):
            raise ValueError(f'{key} 必须是 0–99 的整数')
        if key == 'part_name' and (not isinstance(value, str) or len(value) > 100):
            raise ValueError('配件名称格式不正确')
        result[key] = {'value': value, 'source': source}
        for meta in ('quote', 'message_id'):
            if isinstance(wrapped.get(meta), str):
                result[key][meta] = wrapped[meta][:2000]
    return result


def values(facts):
    return {key: item.get('value') if isinstance(item, dict) else item for key, item in facts.items()}


def causation_evidence(text):
    """Latest asserted conflict/clarification, with its original source span.

    Callers must scope customer statements to one order before combining them.
    Order identifiers are attribution, not words between a correction and its
    causal statement; their length must not change whether a correction works.
    """
    if not isinstance(text,str):text=''
    uncertainty=r'无法(?:确认|确定|判断)|不能(?:确认|确定|判断)|没法(?:确认|判断)|不确定|不清楚|说不[清准]|记不清|先后.{0,4}不清|原因.{0,4}不明|责任.{0,4}不明|前后矛盾'
    incident=r'摔|磕|碰|进水|改装|自己.{0,5}(?:弄坏|损坏|弄丢)'
    cause_words=r'先后|原因|责任|谁的|谁造成|摔|磕|碰|进水|改装|造成|导致|什么时候'
    latest_conflict=None
    unasserted = r'如果|假如|假设|要是|倘若|朋友|别人|客服说|商家说|模板|举例|[“”「」『』\"]'
    for match in re.finditer(uncertainty,text):
        line_start=text.rfind('\n',0,match.start())+1
        line_end=text.find('\n',match.end())
        if line_end<0:line_end=len(text)
        window=text[max(line_start,match.start()-90):min(line_end,match.end()+60)]
        if re.search(unasserted, text[line_start:line_end]):continue
        # Explicit denials must not turn into a history of customer damage.
        cleaned=re.sub(r'(?:没有|从未|没|未)(?:发生过|发生|有过|有|曾|进行过|进行)?(?:摔碰|摔过|摔|磕碰|磕|碰过|碰|进水|改装)(?:过)?','',window)
        cleaned=re.sub(r'不是(?:我|自己).{0,4}(?:摔|弄坏|损坏)','',cleaned)
        if re.search(incident,cleaned) and re.search(cause_words,window):
            latest_conflict=(match.start(),window)
    resolved=[]
    identity = r'(?:\s*(?:订单\s*)?[A-Za-z][A-Za-z0-9_-]{2,}\s*[：:]?\s*)?'
    clarification = (r'(?:已经|已)(?:核实|查明|确认)|(?:现在|后来|核对后|更正|刚才记错了)'
                     + identity + r'[^\n。；;]{0,12}(?:确认|确定|明确|是我)|确认(?:是我|不是我|不是摔|在摔|摔之前)')
    for match in re.finditer(clarification,text):
        line_start=text.rfind('\n',0,match.start())+1
        line_end=text.find('\n',match.end())
        if line_end<0:line_end=len(text)
        if re.search(unasserted, text[line_start:line_end]):continue
        if re.search(r'(?:没(?:有)?|未|不曾|并非|不能|无法|不再|撤回).{0,4}$', text[line_start:match.start()]):continue
        window=text[match.start():min(line_end,match.end()+100)]
        if re.search(uncertainty,window):continue
        if re.search(r'开箱|到货|摔|磕|碰|进水|改装|原因|造成|弄坏',window):resolved.append((match.start(),window))
    if resolved and (latest_conflict is None or resolved[-1][0]>latest_conflict[0]):
        return {'value':False,'source':'customer','quote':resolved[-1][1]}
    elif latest_conflict:
        return {'value':True,'source':'customer','quote':latest_conflict[1]}
    return None


def apply_conflict_guard(facts, text=''):
    """Preserve unresolved causation; only explicit later evidence resolves it."""
    result={key:(dict(item) if isinstance(item,dict) else item) for key,item in facts.items()}
    evidence = causation_evidence(text)
    if evidence:
        result['causation_conflict'] = evidence
    return result


def apply_arrival_correction_guard(facts, text):
    """Remove superseded arrival evidence from the current order projection.

    Customer messages and historical assessments stay immutable. A causal
    correction can withdraw an old defect claim without proving that the item
    was intact on arrival; only an explicit arrival statement supports false.
    Call only with chronologically ordered, single-order customer evidence.
    """
    result = {key: (dict(item) if isinstance(item, dict) else item) for key, item in facts.items()}
    prior = result.get('defect_on_arrival')
    if not isinstance(prior, dict) or not prior.get('quote'):
        return result
    rank = text.rfind(prior['quote'])
    if rank < 0:
        return result
    rank_end = rank + len(prior['quote'])
    blocked = r'如果|假如|假设|要是|倘若|朋友|别人|听说|客服说|商家说|举例|模板|[“”「」『』\"]'
    arrival = r'(?:到货|收到|签收|开箱|一开始|原本)'
    defect = r'(?:坏|损坏|破损|故障|缺陷|质量问题|问题)'
    for match in re.finditer(r'[^\n。；;]+', text):
        line = match.group().strip()
        if match.end() <= rank_end or re.search(blocked, line):
            continue
        if match.start() <= rank:
            # A source quote and its correction can occur in one sentence.
            line = text[rank_end:match.end()].strip('，, ')
        # Denying a correction, asking about one, or withdrawing a statement of
        # *no* defect cannot withdraw the original positive defect statement.
        if re.search(r'(?:没(?:有)?|未|并非|不曾|不能|无法).{0,4}(?:更正|撤回|改口)|[？?]|(?:吗|么)$', line):
            continue
        if re.search(r'(?:不是|并非).{0,12}(?:没(?:有)?|无).{0,5}' + defect, line):
            continue
        explicit_negative = bool(re.search(
            arrival + r'.{0,10}(?:完好|正常|没问题|没有问题|无故障)'
            r'|(?:不是|并非|非).{0,3}' + arrival + r'.{0,5}' + defect, line))
        unknown = bool(re.search(r'不确定|不知道|不清楚|无法确认|不能确认|记不清', line))
        withdrawal = bool(re.search(r'撤回|说错|记错|不属实|更正', line)
                          and re.search(arrival + r'.{0,8}' + defect, line))
        negative_claim = re.search(arrival + r'.{0,8}(?:没有|没|无)(?:任何)?' + defect, line)
        retracted_intact = re.search(
            r'撤回.{0,12}' + arrival + r'.{0,10}(?:完好|正常|没问题|没有问题|无故障)'
            r'|' + arrival + r'.{0,10}(?:完好|正常|没问题|没有问题|无故障).{0,12}(?:说错|记错|不属实|撤回)', line)
        if ((negative_claim and re.search(r'撤回|说错|记错|不属实', line)) or retracted_intact):
            if result.get('defect_on_arrival', {}).get('value') is False:
                result.pop('defect_on_arrival', None)
            withdrawal = False
            explicit_negative = False
        if re.search(r'不是没|不是没有|并非没|不完好|不正常|并非完好|不是完好', line):
            explicit_negative = False
        causal = causation_evidence(line)
        own = re.search(r'(?:我自己|是我|被我|我把|自己).{0,8}(?:摔坏|摔裂|弄坏|压坏|砸坏|泡坏|撕坏)', line)
        denied_own = re.search(r'(?:不是|并非|没(?:有)?|未).{0,8}(?:我|自己|摔|弄)', line)
        denies_quality = re.search(r'(?:不是|并非|非)(?:商品)?质量问题', line)
        correction = bool(causal and causal['value'] is False and own and not denied_own
                          and denies_quality and re.search(r'更正|改口|记错|说错|其实|实际', line))
        # A second, later incident is compatible with the item already having
        # an arrival defect and must not silently erase the earlier evidence.
        separate_incident = re.search(r'后来又|之后又|再次|另外.{0,4}(?:摔|损坏)|仍(?:然)?是.{0,4}到货', line)
        if separate_incident:
            continue
        if explicit_negative and not unknown:
            result['defect_on_arrival'] = {'value': False, 'source': 'customer', 'quote': line}
            rank = match.start()
            rank_end = match.end()
        elif withdrawal or correction or (unknown and re.search(arrival, line)):
            result.pop('defect_on_arrival', None)
            rank = match.start()
            rank_end = match.end()
    return result


def from_text(reason, condition=''):
    """Conservative fallback for forms; unknowns stay unknown and are asked about."""
    text = f'{reason}。{condition}'
    result = {}
    def put(key, value):
        result[key] = {'value': value, 'source': 'customer', 'quote': text[:2000]}
    # Only remove explicitly negated defect predicates, not arbitrary clauses.
    negative = r'(?:并无|并非|不是|不存在|没有|未曾|并未|尚未|不曾|无|没|未)'
    qualifier = r'(?:任何|明显|发现过|出现过|发现|出现|发生|看见|看到|进行|经过){0,2}'
    predicate = r'(?:质量问题|洗涤|水洗|洗过|机洗|手洗|清洗|使用过|使用|穿着|穿过|试穿过|试穿|佩戴过|佩戴|损坏|破损|污损|损伤|故障|异响|漏水|瑕疵|划痕|变形|裂纹|缺件|少件|开裂|破洞|问题|损|坏)'
    item = qualifier + predicate
    clean = re.sub(negative + item + r'(?:\s*(?:、|以及|及|与|或|和|/|／)\s*(?:' + negative + r')?' + item + r')*', '', text)
    own = bool(re.search(r'(?:我|自己|孩子|人为).{0,8}(?:摔|弄|砸|丢|压|扯|撕|泡|洗坏)|使用不当|自行(?:拆|改)|不按说明', clean))
    defect = bool(re.search(r'损坏|破损|坏了|故障|质量问题|异响|漏水|裂纹|开裂|破洞|污损|不能(?:开机|充电|出声|工作|加热)|无法(?:开机|充电|出声|工作|加热)|没有声音|不出声|不开机|不充电|充不进|开不了机|鼓包|(?:杯|屏|壶).{0,3}(?:裂|碎)', clean))
    missing = bool(re.search(r'少(?:了|发|寄|装|件|一|两|\d)|缺(?:少|件)|漏发|漏装|没(?:有)?收到.{0,8}(?:配件|线|充电盒)', clean))
    wrong = bool(re.search(r'发错|寄错|错发|与(?:订单|下单|描述).{0,5}不符', clean))
    if own:
        put('responsibility', 'customer'); put('condition', 'damaged')
    if wrong:
        put('issue_type', 'wrong_item'); put('responsibility', 'seller')
    elif missing:
        put('issue_type', 'missing')
        if not own: put('responsibility', 'seller')
    elif defect:
        put('issue_type', 'damage' if re.search(r'破损|裂|碎|破洞|污损', clean) else 'quality')
    elif re.search(r'不喜欢|不合适|偏小|偏大|想退|退货|换颜色|换.{0,3}(?:码|色)|无理由', text):
        put('issue_type', 'no_reason')
    if defect and re.search(r'收到|到货|签收|开箱|刚拆', text) and not own:
        put('defect_on_arrival', True); put('responsibility', 'seller')
    if re.search(r'正常使用|按(?:照)?说明(?:书)?使用', text) and not own:
        put('normal_use', True)
    if re.search(r'仅(?:拆封|正常拆封|试穿|试用|检查)|只是(?:拆封|试穿)|合理(?:查验|调试)|拆封查验', text) and not own:
        put('condition', 'inspection_only')
    elif re.search(r'完好|无(?:损坏|破损|污损|使用痕迹)|没有(?:损坏|破损)|正常[，,。 ]', text) and not defect and not own:
        put('condition', 'intact')
    if re.search(r'配件(?:都在|齐全|完整)|没有缺件|无缺件', text): put('accessories_complete', True)
    if not own and re.search(r'(?:已经|已|我)(?:穿过|用过|水洗|洗过)|明显(?:使用|污损)|穿了.{0,5}(?:天|次)', clean):
        put('condition', 'used')
    if re.search(r'维修.{0,8}(?:失败|无效|仍|还)|修了.{0,8}(?:还|仍)|无法修复|不能修复', text): put('repair_failed', True)
    if re.search(r'没有维修|没修过|尚未维修|还没修', text): put('repair_failed', False)
    return result


def merge(reason, condition='', supplied=None, *, order_id=None):
    # An explicit extraction (even {}) is authoritative about what is known.
    # Never fill a model's deliberate unknowns using text heuristics.
    result = from_text(reason, condition) if supplied is None else normalize(supplied)
    # The combined basis is server-checked against the current request text;
    # a client-provided fact alone cannot create or preserve it.
    result.pop('unopened_return_basis', None)
    # Keep a literal source span. The separator used by from_text is not
    # customer wording and cannot be part of an evidence quote.
    basis = unopened_return_basis_quote('\n'.join((reason, condition)), order_id=order_id)
    if basis:
        result['unopened_return_basis'] = {'value': True, 'source': 'customer', 'quote': basis}
    return protect_delivery_responsibility(result, '\n'.join((reason, condition)), order_id=order_id)
