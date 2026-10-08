"""Inventory and simulated checkout do not establish real shipping timelines."""
import json
import re


EVENT = r'(?:发货|发出(?:去)?|发走|寄出(?:去)?|寄走|出库|送达|送到|到达|到货|收到货|收货|到手(?!价)|(?<![补启触研揭])发(?=\s*$))'
DATE = (r'(?:今天|今日|今晚|明天|明日|明早|明晚|后天|大后天|当天|当日|次日|次天|'
        r'(?:本|下)(?:周|星期)[一二三四五六日天]?|\d{4}-\d{2}-\d{2}|'
        r'\d{1,2}\s*月\s*\d{1,2}\s*日|[一二两三四五六七八九十\d]+\s*(?:小时|工作日|天|日))')
CLAIM = re.compile(r'(?:将|会|可以|可|能|一般|通常|正常|保证|承诺|预计|安排|应该|大概|就)'
                   r'[^，,；;。！？!?\n]{0,16}' + EVENT + r'|'
                   + DATE + r'[^，,；;。！？!?\n]{0,16}' + EVENT + r'|'
                   + EVENT + r'[^，,；;。！？!?\n]{0,16}' + DATE)
NEGATED = re.compile(r'不(?:能|会|可|支持|保证|承诺)|无法|没法|尚未|未能|没有|不能确定|不代表|不等于|不是|并非|不一定')
NOTICE = '本系统仅记录模拟订单，未接入真实物流，无法确认发货或送达时间'


def receipt_window(clause, claim):
    """Receipt starts an after-sale window; it is not a promised arrival."""
    events = list(re.finditer(EVENT,claim[0]))
    if not events or events[-1][0] not in ('收货','到货','收到货'):
        return False
    # A stated arrival date before receipt is a delivery claim, even when
    # receipt also starts a valid refund or inspection window afterwards.
    if re.search(DATE, claim[0][:events[-1].start()]):
        return False
    tail = clause[claim.start()+events[-1].end():]
    return bool(re.match(r'\s*(?:之后|以后|后)',tail)
                and re.search(r'申请|退货|换货|补发|退款|维修|售后|检查|核对|拍照|包装|联系|上报',tail))


def negates_timing(prefix):
    """An absent circumstance in an if-clause does not deny later dispatch."""
    for denial in NEGATED.finditer(prefix):
        if denial[0] == '没有' and re.search(r'(?:如果|假如|倘若|要是|只要|若)[^，,；;。！？!?\n]{0,12}$', prefix[:denial.start()]):
            condition = re.split(DATE + r'|将|会|可以|能|保证|承诺|预计|就',
                                 prefix[denial.end():], maxsplit=1)[0]
            if condition and not re.search(EVENT + r'|办法|能力|依据|记录|时效|时间|保证|承诺|确认|确定', condition):
                continue
        return True
    return False


def recorded(clause, ctx, facts, reference=''):
    """Allow an attributed, owner-checked record; an estimate is no guarantee."""
    scope = reference + clause
    if not re.search(r'记录|物流信息|物流状态|模拟', scope) or re.search(r'保证|承诺|一定|肯定|下单|付款', clause):
        return False
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', clause)
    if not dates:
        return False
    orders = list(ctx.get('orders', []))
    for fact in facts or []:
        value = fact.get('result')
        if fact.get('tool') == 'get_order' and isinstance(value, dict):
            orders.append(value)
        elif fact.get('tool') == 'get_orders' and isinstance(value, list):
            orders.extend(value)
    for order in orders:
        if (not isinstance(order, dict) or order.get('user_id') != ctx.get('user_id')
                or not order.get('id') or not re.search(r'(?<![A-Za-z0-9_-])' + re.escape(order['id']) + r'(?![A-Za-z0-9_-])', scope)):
            continue
        key_pattern = r'ship|dispatch|outbound|fulfill' if re.search(r'发货|发出|寄出|出库|发走|寄走', clause) else r'deliver|arrival|logistic|tracking'
        fields = {key:value for key,value in order.items() if re.search(key_pattern, key, re.I)}
        evidence = json.dumps(fields, ensure_ascii=False)
        if fields and all(date in evidence for date in dates):
            return True
    return False


def ground_response(text, ctx, facts=None):
    """Keep stock/restock facts while withholding unsupported delivery times.

    Conditional checkout promises are claims too. Customer quotations and
    attributed actual order records are checked separately, not blanket
    exemptions for words such as 'if', 'normally', or 'estimated'.
    """
    literals = []
    user_text = '\n'.join(m.get('content', '') for m in ctx.get('messages', []) if m.get('role') == 'user')
    def protect(match):
        prefix = text[max(0,match.start()-40):match.start()]
        content = match[0][1:-1]
        if content and content in user_text and re.search(r'(?:你|您|顾客)(?:刚才|之前)?(?:说|问|写|提到)|顾客原话', prefix):
            literals.append(match[0]); return f'＄顾客引文{len(literals)-1}＄'
        return match[0]
    masked = re.sub(r'“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|"[^"\n]*"', protect, text)
    parts = re.split(r'([，,；;。！？!?\n])', masked)
    reference = ''
    notice_inserted = False
    duplicate_removed = False
    for index in range(0,len(parts),2):
        clause = parts[index]
        if not clause.strip() and index and parts[index-1] == '\n':
            reference = ''
            notice_inserted = False
        claims = list(CLAIM.finditer(clause))
        if not claims:
            if re.search(r'记录|物流信息|物流状态|模拟', clause):
                reference = clause
            continue
        # A supplement about estimated restocking is a catalogue fact, not
        # a shipment to this customer. Do not convert it into delivery prose.
        if re.search(r'补货|补货日期|补货时间', clause) and not re.search(r'发货|发出|寄出|出库|送达|送到|收到货', clause):
            continue
        unsupported = False
        for claim in claims:
            if receipt_window(clause,claim):
                continue
            uncertain_tail = re.match(r'\s*(?:(?:尚|仍|还)?(?:不能|无法|没法)(?:保证|确定|确认|承诺)|'
                                      r'(?:尚未|未)(?:确认|核实)|不确定|不一定)',clause[claim.end():])
            prefix = re.split(r'但(?:是)?|不过|却|而是|而且|同时',clause[:claim.end()])[-1]
            if negates_timing(prefix) or uncertain_tail or recorded(clause, ctx, facts, reference):
                continue
            unsupported = True
            break
        if not unsupported:
            continue
        if notice_inserted:
            parts[index] = ''
            if index and parts[index-1] in ('，',',','；',';'):
                parts[index-1] = ''
            duplicate_removed = True
        else:
            parts[index] = NOTICE
            notice_inserted = True
    result = ''.join(parts)
    if duplicate_removed:
        result = re.sub(r'([。！？!?])[，,；;]|([。！？!?])\2+',lambda m:m[1] or m[2],result)
    for index,literal in enumerate(literals):
        result = result.replace(f'＄顾客引文{index}＄',literal)
    return result
