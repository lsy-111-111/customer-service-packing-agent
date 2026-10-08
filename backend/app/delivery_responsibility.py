"""A signed-delivery record and zero actual receipt do not establish liability.

This module checks customer-source spans only. Its chronology helper must be
given one order's statements; form callers also supply their owned order ID.
An explicit seller omission or the customer's own later loss remains distinct
from a disputed carrier receipt. No investigation result is invented here.
"""
import re


_SIGNED = re.compile(
    r'(?:系统|物流|配送|快递|平台|记录|状态).{0,16}'
    r'(?:已签收|签收成功|(?:显示|标记|记录)(?:为|已|已经)?签收|已送达|已投递)'
    r'|(?:显示|标记)(?:为|已|已经)?(?:签收|送达|投递)')
_ZERO_RECEIPT = re.compile(
    r'(?:实际收到|实收|收到|收货数量)(?:了|的|商品|货物|数量|为|是|共|只有|仅有|有|\s){0,5}'
    r'[0零〇]\s*(?:件|个|副|台|双|包|箱|套|支|瓶|根|条|只)'
    r'|(?:一(?:件|个|副|台|包|箱|套|只)(?:也|都)?|完全|根本|什么都)?没(?:有)?收到'
    r'|未收到|并非本人签收|不是我签收')
_NONASSERTION = re.compile(
    r'如果|假如|假设|要是|倘若|朋友|别人|另一(?:单|笔|个订单)|其他订单|一般知识|举例|引用|模板|[“”‘’「」『』\"]'
    r'|(?:没有|没|未|并未)(?:说|表示).{0,12}(?:系统|物流|签收|收到)')
_SELLER = re.compile(
    r'(?:商家|卖家|店铺|客服)(?:已(?:经)?|确实)?(?:确认|承认|核实)(?:了)?'
    r'[^，,。；;\n]{0,12}(?:漏发|少发|欠发|未发货|没发货|未寄出|没寄出)'
    r'|(?:商家|卖家|店铺)(?:确实)?(?:漏发|少发|欠发|未发货|没有发货)')
_CUSTOMER = re.compile(
    r'(?:我自己|是我|我把|被我|自己).{0,8}(?:弄丢|丢失|遗失|丢了)')
_UNCONFIRMED = re.compile(
    r'不确定|不清楚|不知道|无法确认|不能确认|尚未确认|未确认|没有确认|没确认'
    r'|(?:没有|没|未|并未).{0,4}(?:确认|承认|核实)|[？?]|[吗么]$')


def _status_text(source):
    # Quoting the literal status word is still the customer's description of
    # the record. Quotes around an incident, hypothetical or instruction keep
    # their ordinary nonassertion boundary.
    return re.sub(r'[“‘\'\"「『](?:已|已经)?(?:签收|送达|投递)[”’\'\"」』]',
                  lambda match: match[0][1:-1], source)


def _basis(clause):
    if _NONASSERTION.search(_status_text(clause)) or _UNCONFIRMED.search(clause):
        return None
    if re.search(r'撤回|说错|记错|不属实|并非|不是|(?:没有|没|未)(?:少发|漏发|欠发)', clause):
        return None
    if _SELLER.search(clause):
        return 'seller'
    if _CUSTOMER.search(clause):
        return 'customer'
    return None


def _sources(text, order_id=None):
    previous, focus = None, set()
    for match in re.finditer(r'[^。；;\n]+', text):
        source = match.group().strip()
        if not source or _NONASSERTION.search(_status_text(source)):
            previous = None
            continue
        identities = {str(m.group(1) or m.group()).casefold() for m in re.finditer(
            r'(?<![A-Za-z0-9_-])(?:DEMO|EVAL|ORDER)[_-][A-Za-z0-9_-]+'
            r'|订单\s*([A-Za-z0-9][A-Za-z0-9_-]*)', source, re.I)}
        if identities:
            focus = identities
        if order_id and focus and focus != {order_id.casefold()}:
            previous = None
            continue
        yield match.start(), source
        if (previous and _SIGNED.search(_status_text(previous[1]))
                and _ZERO_RECEIPT.search(source)
                and len(previous[2] | focus) <= 1):
            # Preserve the exact source punctuation while combining adjacent
            # status and receipt sentences about the same order.
            yield previous[0], text[previous[0]:match.end()].strip()
        previous = (match.start(), source, set(focus))


def disputed_responsibility(item, text):
    """A cropped receipt quote cannot remove its disputed-signature context."""
    if item.get('value') not in ('seller', 'customer'):
        return item
    quote = item.get('quote', '')
    if _basis(quote) == item['value']:
        return item
    for _, source in _sources(text):
        if quote and quote.rstrip('。；;\n') in source and _SIGNED.search(_status_text(source)) and _ZERO_RECEIPT.search(source):
            return {**item, 'value': 'unknown', 'quote': source, 'source': 'customer'}
    return item


def protect_delivery_responsibility(facts, text, *, order_id=None):
    """Merge latest literal causal clarification after resolving order scope.

    A neutral repetition of the receipt dispute does not retract an explicit
    seller confirmation. A subsequent causal withdrawal does. The oldest
    unsupported seller tag is never a substitute for a causal statement.
    """
    result = {key: dict(item) if isinstance(item, dict) else item for key, item in facts.items()}
    sources = list(_sources(text, order_id))
    disputes = [(start, source) for start, source in sources
                if _SIGNED.search(_status_text(source)) and _ZERO_RECEIPT.search(source)]
    if not disputes:
        return result
    events = []
    for start, source in sources:
        if _SIGNED.search(_status_text(source)) and (receipt := _ZERO_RECEIPT.search(source)):
            events.append((start + receipt.start(), 'dispute', source))
        for clause_match in re.finditer(r'[^，,]+', source):
            clause = clause_match.group().strip()
            basis = _basis(clause)
            if basis:
                events.append((start + clause_match.start(), basis, clause))
            elif (re.search(r'责任|商家|卖家|漏发|少发|欠发', clause)
                  and re.search(r'撤回|说错|记错|不属实|责任(?:不明|未定|待核实|尚未核实)'
                                r'|(?:不确定|不清楚|不知道|无法确认|不能确认).{0,12}(?:责任|谁|商家|卖家|漏发|少发)'
                                r'|(?:商家|卖家|漏发|少发).{0,12}(?:尚未确认|未确认|没有确认|没确认)', clause)
                  and not re.search(r'(?:没有|没|未|并未|不是).{0,4}(?:撤回|说错|记错)', clause)):
                events.append((start + clause_match.start(), 'unknown', clause))
    current = None
    for _, value, quote in sorted(events):
        if value == 'dispute':
            if not current or current['value'] == 'unknown':
                current = {'value': 'unknown', 'source': 'customer', 'quote': quote}
        else:
            current = {'value': value, 'source': 'customer', 'quote': quote}
    if current:
        result['responsibility'] = current
    return result
