"""Keep a multi-product checkout claim separate from independent SKU quotes."""
import re

_COLLECTIVE = r'(?:上述|以上|前面|这)?(?:[二两三四五六七八九十\d]+|多|全部|所有)(?:项|款|种商品|件商品|个规格)(?:商品)?'
_CART = r'(?:合单|购物车|组合(?:购买|结算)?|一起(?:购买|结算|下单))'
_TOTAL = re.compile(
    rf'(?P<subject>{_COLLECTIVE}|{_CART})[^。！？；;\n]{{0,30}}?'
    r'(?:合计|总计|总额|总价|一共|总共|实付)(?:实付|总额|总价)?'
    r'\s*(?:(?:是|为|约|共)|[:：])?\s*[¥￥]?\s*(?:\*\*)?\d+(?:\.\d+)?\s*元(?:\*\*)?')


def guard_combined_payable(text, *, multi_quote_context=False, product_count=None):
    """No cart pricing tool exists, so don't borrow a last item's payable.

    A named item's quantity total remains untouched. Collective numerical
    checkout claims are replaced with their actual evidence boundary; we do
    not add independently discounted line quotes as a promised cart discount.
    Quoted, hypothetical, negated and historic claims retain their own scope.
    """
    parts = re.split(r'(?<=[。！？；;\n])', text)
    offset = 0
    for i, part in enumerate(parts):
        start = offset
        offset += len(part)
        match = None
        collective_heading = False
        for candidate in _TOTAL.finditer(part):
            # A shortlist heading does not own the next named item's paid
            # price. Keep the whole item, then still inspect later collective
            # or named aggregate claims in the same sentence.
            item_under_heading = bool(product_count and product_count(candidate.group()) == 1
                and not re.search(_CART, candidate['subject'])
                and re.search(r'(?:推荐|选项|供(?:比较|参考))\s*[:：]', candidate.group())
                and not re.search(r'合计|总计|总额|总价|一共|总共', candidate.group()))
            if not item_under_heading:
                match = candidate
                break
            collective_heading = True
        named_aggregate = False
        if not match and product_count and (product_count(part) > 1
                or collective_heading and multi_quote_context):
            match = re.search(r'(?:合计|总计|总额|总价|一共|总共)(?:实付)?\s*(?:为|是|约|共|[:：])?\s*[¥￥]?\s*\d+(?:\.\d+)?\s*元', part)
            named_aggregate = bool(match and (collective_heading and multi_quote_context
                or not re.search(r'分别|各自|各|均', part[:match.start()])))
            if not named_aggregate:
                match = None
        if not match:
            continue
        if not multi_quote_context and not named_aggregate and not re.search(_CART, match['subject']):
            continue
        if re.search(r'分别|各自|各|均|每|都|并非|不是|不能|不代表|不等于', match.group()):
            continue
        prefix = part[:match.start()]
        global_prefix = text[:start + match.start()]
        if (re.search(r'如果|假设|假如|若是|假定|并非|不是|不能说|不代表|不要说|不应|原订单|旧订单|历史|当时|曾经|之前|上次', prefix)
                or re.search(r'不等于|不正确|错误|不准确|吗|[？?]', part[match.end():])
                or any(global_prefix.count(left) > global_prefix.count(right) for left, right in [('“', '”'), ('‘', '’'), ('「', '」'), ('『', '』')])
                or global_prefix.count('"') % 2):
            continue
        replacement = '合并结算的总额和优惠尚未核实，不能以单项报价代替'
        parts[i] = part[:match.start()] + replacement + part[match.end():]
    return ''.join(parts)
