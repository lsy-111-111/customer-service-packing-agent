"""Keep proposed after-sale routes consistent with an owned-order admission.

This is a read-only publication guard. It does not inspect goods, decide an
application, create a case, or turn a customer statement into a staff finding.
Order resolution and chronological customer evidence use the caller's existing
source-scoping helpers; assistant prose and old cards cannot establish damage.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from . import conversation_state
from .fact_extractor import validate_facts
from .facts import causation_evidence


_ADMISSION = re.compile(
    r'(?:是我|我自己|被我|我把|由我|本人|自己|我)(?:自己)?'
    r'(?:不小心|亲手|无意间|不慎|已经|刚才|后来|确实|给)?'
    r'(?:摔坏|摔裂|摔碎|弄坏|压坏|砸坏|泡坏|撕坏)')
_UNASSERTED = re.compile(
    r'如果|假如|假设|要是|倘若|朋友|别人|听说|客服说|商家说|他说|她说|'
    r'引用|举例|模板|[“”「」『』\"]')
_CAUSE = r'自损|自行损坏|自己损坏|自己摔|我摔|我造成|责任|原因|摔坏|弄坏|损坏|商品本体|刚才那句'
_UNCERTAIN = re.compile(
    r'(?:不确定|不清楚|不知道|无法确认|不能确认|记不清|尚未确认).{0,20}(?:' + _CAUSE + r')'
    r'|(?:' + _CAUSE + r').{0,20}(?:不确定|不清楚|不明|无法确认|不能确认|未知)')
_WITHDRAWAL = re.compile(
    r'(?:撤回|说错|记错|不属实|更正).{0,30}(?:' + _CAUSE + r')'
    r'|(?:' + _CAUSE + r').{0,30}(?:撤回|说错|记错|不属实)')
_DENIED = re.compile(
    r'(?:不是|并非|没有|没|未).{0,8}(?:我|自己).{0,8}(?:摔|弄坏|造成|导致)'
    r'|(?:商品|产品|本体).{0,8}(?:没有|没|未|无)(?:任何)?(?:损坏|破损)')
_ROUTE = re.compile(
    r'(?:无理由|七[日天]|7\s*[日天]).{0,8}(?:退货|退款|换货)'
    r'|非质量(?:问题)?(?:的)?(?:退换货|换货|退货)')
_INVITATION = re.compile(
    r'想(?:走|选择|申请|办理|退|换)|要不要|是否(?:需要|想|要)|需要我|'
    r'倾向|打算|愿意|选择|考虑|尝试|(?:仍|还|也)可|可(?:以|走|申请|办理|行使)|'
    r'能(?:走|申请|办理|行使)|如需|先(?:帮|为).{0,12}(?:申请|草稿)|还是|或者')
_NEGATIVE_ROUTE = re.compile(
    r'不能|不可|不应|不得|不要|不建议|不符合|不满足|不适用|不支持|不等于|不代表|'
    r'被拒|拒绝|不保证|无法|不得以|不能以')
_FOLLOWUP = re.compile(
    r'^(?:请|再|你|您|可以|麻烦)?(?:告诉我|说一下|说明).{0,15}'
    r'(?:方向|选择|哪种|哪一(?:种|条)|路线|想法)'
    r'|^(?:需要我|我可以|我(?:先)?帮你|我(?:先)?为你|先帮你|先为你).{0,18}'
    r'(?:具体资格|看看资格|确认资格|准备什么|需要准备|退货草稿|换货草稿|整理.{0,6}草稿|整理.{0,6}申请)'
    r'|^(?:你|您)?(?:倾向|想选择|想选|想走|选择).{0,10}(?:哪|哪种|哪一|方向|路线)'
    r'|^(?:如果|若)(?:你|您)?想走(?:这|该)条路'
    r'|^(?:告诉我|说明)(?:即可|便可|就可|就能).{0,18}(?:草稿|申请|拟)'
    r'|^(?:要不要|是否需要)(?:我)?(?:先)?(?:拟|整理).{0,12}(?:退货|换货|草稿|着)')
_INTACT_CONDITION = re.compile(
    r'^\s*(?:只要|如果|若|需(?:要)?|须|前提是)(?:商品|产品|本体)?(?:仍|保持)?(?:完好|完整)[）)\s]*$')
_INDEPENDENT = re.compile(
    r'另外|另问|顺便|同时|维修|人工复核|复核|新买|另买|新购|购买|报价|实付|价格|'
    r'库存|尺寸|尺码|颜色|容量|参数|功能')
_EXPLANATION = ('根据你对本单的说明，商品由你自行损坏，不能按商品完好的前提引导无理由退货或非质量换货；'
                '如需后续处理，可咨询维修或人工复核。')


def _other_object_pattern(ctx: dict, order: dict) -> str:
    names = {'包装', '外箱', '外盒', '赠品', '配件', '附件', '说明书', '吊牌', '标签'}
    product = next((p for p in ctx.get('products', []) if p.get('id') == order.get('product_id')), {})
    for part in [*order.get('package_snapshot', []), *product.get('parts', [])]:
        if part.get('kind') == 'main':
            continue
        if isinstance(part.get('name'), str) and len(part['name'].strip()) >= 2:
            names.add(part['name'].strip())
    return '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True))


def _confirmed_damage(ctx: dict, order: dict, customer_text: str, facts: Iterable[dict]) -> bool:
    # Different units can have different causes and conditions. A whole-order
    # route cannot be constrained by an unbound observation about one unit.
    if not customer_text or order.get('quantity', 1) != 1:
        return False
    other = _other_object_pattern(ctx, order)
    product = next((p for p in ctx.get('products', []) if p.get('id') == order.get('product_id')), {})
    main = '|'.join(re.escape(s) for s in {'商品', '产品', '本体', '主机', product.get('name', '')} if s)
    confirmed_at = -1
    arrival_conflict = False
    for span in re.finditer(r'[^。；;！？!?\n]+', customer_text):
        statement = span.group()
        if _UNASSERTED.search(statement):
            continue
        if (_WITHDRAWAL.search(statement) or _DENIED.search(statement) or _UNCERTAIN.search(statement)):
            confirmed_at = -1
        arrival = re.search(r'(?:到货|收到|开箱).{0,6}(?:就|已|已经)?(?:坏|裂|破损|损坏)', statement)
        if arrival and not re.search(r'以为|猜测|推测|不是|并非|撤回|记错|说错', statement[:arrival.end()]):
            arrival_conflict = True
        for admission in _ADMISSION.finditer(statement):
            before, after = statement[:admission.start()], statement[admission.end():]
            if (re.search(r'(?:不是|并非|没(?:有)?|未|没说|没有说).{0,10}$', before)
                    or re.match(r'(?:的|了)?\s*(?:吗|么|[？?])', after)
                    or _UNCERTAIN.search(statement) or _DENIED.search(statement)):
                continue
            other_subjects = list(re.finditer(other, before, re.I))
            main_subjects = list(re.finditer(main, before))
            if other_subjects and (not main_subjects or other_subjects[-1].start() > main_subjects[-1].start()):
                continue
            if re.search(other, re.split(r'[，,]', after)[0], re.I):
                continue
            # Use the existing evidence validator without cropping governing
            # negation or conditional context away from the statement.
            checked = validate_facts({
                'responsibility': {'value': 'customer', 'quote': statement},
                'condition': {'value': 'damaged', 'quote': statement},
                'issue_type': {'value': 'damage', 'quote': statement},
            }, customer_text, conflict_text=statement)
            if not all(checked.get(k, {}).get('value') == v for k, v in (
                    ('responsibility', 'customer'), ('condition', 'damaged'), ('issue_type', 'damage'))):
                continue
            if re.search(r'已(?:经)?确认|现(?:在)?已确认|现在确认|更正|实际|确实', before):
                if not re.search(r'后来又|之后又|再次', statement):
                    arrival_conflict = False
            confirmed_at = span.start() + admission.start()
    causal = causation_evidence(customer_text)
    if confirmed_at < 0 or arrival_conflict or (causal and causal.get('value') is True):
        return False
    # A fresh contradictory fact with a later, actual source defeats an old
    # admission. Tool/card creation order alone never beats customer chronology.
    for entry in facts:
        if entry.get('tool') not in ('evaluate_after_sale', 'draft_case'):
            continue
        result = entry.get('result')
        if not isinstance(result, dict) or result.get('order_id') != order.get('id'):
            continue
        raw = result.get('facts') or result.get('assessment', {}).get('facts_used', {})
        for key, item in raw.items():
            if key not in ('responsibility', 'condition', 'causation_conflict', 'defect_on_arrival') or not isinstance(item, dict):
                continue
            quote = item.get('quote', '')
            if not quote or customer_text.rfind(quote) < confirmed_at:
                continue
            checked = validate_facts({key: item}, customer_text, conflict_text=quote)
            value = checked.get(key, {}).get('value')
            if (key == 'responsibility' and value in ('seller', 'unknown')
                    or key == 'condition' and value in ('intact', 'inspection_only')
                    or key in ('causation_conflict', 'defect_on_arrival') and value is True):
                if value != 'unknown' or _UNCERTAIN.search(quote):
                    return False
    return True


def _invites_ineligible_route(clause: str, following: str = '') -> bool:
    if re.search(r'[“”「」『』\"]|你(?:问|说)|引用|举例', clause):
        return False
    if re.search(r'^\s*(?:一般|通常)(?:的)?(?:规则|条件|情况)', clause):
        return False
    for route in _ROUTE.finditer(clause):
        before = re.split(r'但是|不过|但', clause[:route.start()])[-1]
        after = clause[route.end():]
        polarity_before = re.sub(r'要不要', '是否想', before)
        if (_NEGATIVE_ROUTE.search(polarity_before)
                or re.search(r'不符合|不满足|不适用|不能(?:按|办理|套用)|被拒|拒绝', after)):
            continue
        # Review of a refusal is different from inviting the refused route.
        if re.search(r'人工.{0,8}(?:复核|审核)|(?:复核|申诉).{0,10}(?:资格|决定)', before):
            continue
        if (_INVITATION.search(before + after)
                or re.match(r'\s*(?:你|您)?(?:倾向|想选择|想选|想走|选择).{0,10}(?:哪|哪种|哪一)', following)
                or re.search(r'只要.{0,12}完好|仍.{0,10}(?:可以|能)|需要我.{0,12}草稿', clause)):
            return True
    return False


def ground_response(text: str, ctx: dict, question: str, *,
                    resolve_orders: Callable[[dict, str], list[str]],
                    customer_text_for_order: Callable[[dict, str], str],
                    facts: Iterable[dict] = ()) -> str:
    """Prune incompatible choices for confirmed self-damage of the current order.

    Pass agent.referenced_orders and agent.order_customer_text directly; both
    callbacks accept (ctx, value). Invoke after generated prose/supplements and
    before publication. This function never mutates ctx, facts, or records.
    """
    if not isinstance(text, str) or not text or not _ROUTE.search(text):
        return text
    if not re.search(r'订单|这单|本单|那单|这笔|原因|责任|退货|退款|退换|换货|补发|维修|售后|摔坏|弄坏|损坏', question):
        return text
    owned = [o for o in ctx.get('orders', []) if o.get('id')
             and (not o.get('user_id') or o.get('user_id') == ctx.get('user_id'))]
    scoped_ctx = {**ctx, 'orders': owned}
    current = list(dict.fromkeys(resolve_orders(scoped_ctx, question)))
    targets = {order['id']: order for order in owned if order['id'] in current}
    evidence = tuple(facts)
    damaged = {oid for oid, order in targets.items()
               if _confirmed_damage(scoped_ctx, order, customer_text_for_order(scoped_ctx, oid), evidence)}
    if not damaged:
        return text
    focus = current[0] if len(current) == 1 else None
    output = []
    changed = False
    explained = set()
    blocked_followup = False
    # Keep the existing punctuation when no change is required. Commas split
    # independent price/repair facts away from a contradictory route invitation.
    chunks = re.findall(r'[^，,。；;！？!?\n]+[，,。；;！？!?\n]*|[，,。；;！？!?\n]+', text)
    for index, chunk in enumerate(chunks):
        clause = chunk.rstrip('，,。；;！？!?\n')
        ending = chunk[len(clause):]
        explicit = list(dict.fromkeys(m[2] for m in conversation_state._order_mentions(clause, owned)))
        if conversation_state._has_unknown_order(clause, owned):
            focus, blocked_followup = None, False
        elif explicit:
            focus = explicit[0] if len(explicit) == 1 else None
            blocked_followup = False
        else:
            # Actual product names can delimit another owned order, including
            # headings that omit the order ID. Resolver guesses are not enough:
            # require a literal catalog identity in this response clause.
            products = [p for p in ctx.get('products', []) if any(
                isinstance(name, str) and len(name) >= 2 and re.search(
                    (r'(?<![A-Za-z0-9_-])' + re.escape(name) + r'(?![A-Za-z0-9_-])') if name.isascii() else re.escape(name), clause, re.I)
                for name in [p.get('name'), p.get('model'), p.get('id'), *(p.get('aliases') or [])])]
            product_orders = {o['id'] for o in owned if o.get('product_id') in {p.get('id') for p in products}}
            if product_orders:
                focus = next(iter(product_orders)) if len(product_orders) == 1 else None
                blocked_followup = False
        if focus not in damaged:
            output.append(chunk)
            continue
        following = chunks[index + 1] if index + 1 < len(chunks) and re.fullmatch(r'[，,]+', ending) else ''
        normalized = clause.lstrip(' ）)（(').strip()
        linked_condition = bool(_INTACT_CONDITION.fullmatch(normalized) and following
                                and _invites_ineligible_route(following.rstrip('，,。；;！？!?\n')))
        if _invites_ineligible_route(clause, following) or linked_condition:
            changed, blocked_followup = True, True
            if focus not in explained:
                output.append(_EXPLANATION if len(current) == 1 else f'订单 {focus}：' + _EXPLANATION)
                explained.add(focus)
            # A valid repair/review alternative can share the same question.
            for alternative in re.split(r'还是|或(?:者)?', clause):
                if re.search(r'维修|人工复核', alternative) and not _ROUTE.search(alternative):
                    alternative = alternative.strip('？?。 ，,')
                    if alternative:
                        output.append(alternative + '可继续咨询，方案需由人工核实。')
            continue
        if (blocked_followup and (_FOLLOWUP.search(normalized) or _INTACT_CONDITION.fullmatch(normalized))
                and not _INDEPENDENT.search(clause)):
            changed = True
            continue
        output.append(chunk)
        if clause.strip() and _INDEPENDENT.search(clause):
            blocked_followup = False
    if not changed:
        return text
    result = ''.join(output)
    result = re.sub(r'([。！？!?])[，,；;]+', r'\1', result)
    result = re.sub(r'[，,]+(?=\n|$)', '', result)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result or _EXPLANATION
