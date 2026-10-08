"""Bound follow-up questions to the operation the customer actually requested.

This module never supplies catalogue facts, creates business work, or recovers
preferences from assistant prose. Unrecognized requests retain the existing
clarification path; recognized read-only operations cannot become a new sale
or an eligibility questionnaire just because an unrelated field is unknown.
"""
from __future__ import annotations

import re

from . import conversation_state, shopping_state, request_fragments


def _positive_request(text):
    # Denying/quoting an operation is not requesting that operation. Preserve
    # the rest of the clause so "not eligibility, only the amount" stays usable.
    text = request_fragments.without_denied_service_requests(text)
    text = re.sub(r'[“「][^”」]*[”」]', '', text)
    return re.sub(
        r'(?:不(?:要|用|问|需要|申请|办理)|无需|不是(?:要|想)?(?:问|申请|办理)|'
        r'不(?:评估|判断|考虑))[^，,。；;？?\n]*(?:资格|能否|能不能|审批|申请|办理|草稿|原因|责任)'
        r'[^，,。；;？?\n]*', '', text)


def refund_basis_requested(text):
    text = _positive_request(text)
    return bool(re.search(
        r'退款.{0,12}(?:依据|金额|计算|怎么算|如何算|按什么|多少|分摊)|'
        r'退.{0,14}(?:按什么|依据|多少(?:钱|元)?|金额|怎么(?:计算|算))|'
        r'(?:原实付|原订单实付|购买时实付).{0,12}(?:分摊|退款|退)', text))


def refund_basis_only(text):
    """Amount/formula advice is separate from deciding eligibility or filing.

    "Only consulting whether I can return" still requires an assessment.
    A conditional return quantity merely supplies the amount calculation.
    """
    text = _positive_request(text)
    if not refund_basis_requested(text):
        return False
    eligibility = re.search(
        r'资格|符合.{0,6}条件|同意|拒绝|批准|判(?:定|断)|评估|'
        r'能否.{0,8}(?:退|换|修)|能不能.{0,8}(?:退|换|修)|'
        r'(?:可以|可否|是否(?:能|可以)?).{0,8}(?:退|换|修)(?!款.{0,5}(?:计算|金额))|'
        r'能退(?!多少|几|金额)|能换|能修', text)
    filing = re.search(
        r'(?:申请|办理|提交|整理|创建|起草|生成).{0,10}(?:退货|退款|申请|草稿)|'
        r'(?:退货|退款).{0,6}(?:申请|办理|提交)|确认提交', text)
    other_remedy = re.search(r'换货|补发|维修|保修|仅退款|只退款', text)
    return not bool(eligibility or filing or other_remedy)


def refund_calculation_without_catalogue_query(text):
    """Purchase-time amounts are evidence for a refund, not a current quote.

    Keep explicit shopping, current-price/stock, and comparison clauses as
    independent work. Historic order totals or denied shopping requests do
    not renew a prior shopping topic merely because they say "实付".
    """
    if not refund_basis_only(text):
        return False
    for clause in re.split(r'[，,。；;！？?\n]|另外|此外|顺便|同时|但是|不过|但',
                           _positive_request(text)):
        if re.search(r'(?:不要|不用|无需|不想|不问|不查|不看|不按|别).{0,8}'
                     r'(?:买|购买|选|推荐|价|售价|实付|报价|优惠|库存|有货|缺货|补货)', clause):
            continue
        historical = bool(re.search(r'原来|原(?:订单|实付|售价)|当时|以前|之前|'
                                    r'购买时|下单时|买过|买了', clause))
        action = re.search(r'想买|要买|我买|我选|想选|选购|帮我选|请帮我选|'
                           r'该选|推荐|预算|怎么选|如何选|购买(?!时|过|了)', clause)
        if action and not historical:
            return False
        current_field = re.search(r'(?:现在|当前|目前|今天|今日).{0,14}'
                                  r'(?:价格|报价|售价|实付|到手价|优惠|库存|有货|缺货|补货)', clause)
        fields = re.search(r'多少钱|价格|报价|售价|实付|到手价|优惠|库存|有货|缺货|补货', clause)
        if current_field and not refund_basis_requested(clause):
            return False
        if fields and not historical and not refund_basis_requested(clause) and '订单' not in clause:
            return False
    return True


def category_products(products, category):
    """Match a requested type, resolving broad clothing through shared scope.

    A general clothing goal uses the existing 服饰 taxonomy. Concrete types
    still need their own name/alias evidence: an earphone does not prove that
    the same 数码 department contains a mouse or keyboard.
    """
    scope = conversation_state._scope(category, [], '会话')
    broad_clothing = category in ('衣服', '服饰')
    # The generic customer type "杯子" also has direct name/alias evidence
    # in "陶瓷杯/玻璃杯". Do not infer the whole household department or
    # accidentally classify a separately sold cup lid/sleeve as a cup.
    cup_name = re.compile(r'(?:陶瓷|玻璃|水|马克|早餐|咖啡|保温|茶|随行)杯(?!盖|套|垫|架|刷|托|座)')
    return [product for product in products if
            any(category in str(value) for value in
                [product.get('name', ''), product.get('category', ''),
                 *product.get('aliases', [])])
            or (category == '杯子' and any(cup_name.search(str(value)) for value in
                [product.get('name', ''), *product.get('aliases', [])]))
            or (broad_clothing and scope != '会话' and product.get('category') == scope)]


def _current_missing_category(ctx, question, named_products):
    goal = ctx.get('preferences', {}).get('shopping', {}).get('goal') or {}
    category = goal.get('category')
    if named_products:
        return False
    last = next((m for m in reversed(ctx.get('messages', []))
                 if m.get('role') == 'user' and m.get('status') not in ('failed', 'cancelled')), {})
    if not category or goal.get('source_message_id') != last.get('id'):
        # A current explicit category selection need not already be persisted
        # by the preference router (e.g. "想选键盘，办公室用"). Recover only
        # that current action; a remembered goal or a category mentioned in a
        # hypothetical/negative/information clause cannot authorize this path.
        selected = set()
        for clause in re.split(r'[，,。；;！？?\n]', _positive_request(question)):
            if re.search(r'不(?:想|要|选|买)|别|不要|如果|假如|假设|朋友|别人|以前|之前|买过', clause):
                continue
            match = re.search(r'(?:想选|我选|想买|要买|选购|帮我选|推荐)\s*'
                              r'(?P<category>' + shopping_state.CATEGORIES + r')', clause)
            if match:
                selected.add(match['category'])
        if len(selected) != 1:
            return False
        category = next(iter(selected))
    if re.search(r'改看|改买|换看|也(?:看|买)|另(?:外)?(?:看|买)|替代|其他品类|别的品类', question):
        return False
    products = ctx.get('products', [])
    if not products or ctx.get('catalog_evidence', {}).get('source') != 'fresh_database':
        return False
    return not bool(category_products(products, category))


_ASK = re.compile(
    r'请(?:问|(?:你|您)?(?:先|再)?(?:告诉|提供|说|说明|描述|补充|选))|'
    r'(?:麻烦|方便).{0,8}(?:告诉|提供|说|描述|补充)|告诉我|发我|'
    r'(?:你|您)(?:更|最)?(?:想|要|偏好|喜欢|看重)|需要我|需要哪|'
    r'(?:如|如果)(?:你|您).{0,8}(?:有|想)|(?:也|再|顺便)(?:说|告诉|提供)')
_CHOICE_FIELDS = re.compile(
    r'订单号|哪(?:一)?笔|哪(?:一)?单|订单|故障|原因|具体问题|售后|'
    r'颜色|色号|尺码|身高|体重|规格|预算|价位|用途|场景|'
    r'功能|降噪|佩戴|哪(?:一)?款|型号|材质|容量|哪(?:一)?类|品类')
_OPTIONAL_SALES = re.compile(
    r'(?:我|我们)(?:可以|会|再|帮).{0,35}(?:帮你找|帮你选|筛选|推荐|挑选|款式|款型)|'
    r'(?:如需|如果(?:你|您)?想).{0,20}(?:选购|购买|找本店|外观接近)')


def catalogue_method_only(question):
    """A generic unknown-record procedure is not a purchase selection.

    Only the current affirmative method request is used. Independent orders,
    shopping, price/stock, or a specific comparison stay in the mixed path.
    """
    clauses = re.split(r'[，,。；;！？?\n]|另外|此外|顺便|同时|但是|不过|但',
                       _positive_request(question))
    current = []
    for clause in clauses:
        denied = re.match(r'\s*(?:我|这次|本轮|现在|暂时)?(?:'
                          r'(?:不想|不要|不需要|不用|不打算|不再)(?:再)?'
                          r'(?:选购|购买|下单|购物|推荐|选|买)|'
                          r'不是(?:再|要|想)?(?:问|讨论)(?:选购|购物|推荐|售后))', clause)
        # A simple explicitly denied shopping clause is not current shopping.
        # If its unpunctuated remainder has another independent action, keep
        # the whole clause and conservatively take the mixed path instead.
        if denied and not re.search(r'想|要|需|帮|请|查|看|咨询|问|推荐|购买|买|下单',
                                    clause[denied.end():]):
            continue
        current.append(clause)
    text = '，'.join(current)
    if not re.search(r'商品|产品|资料|参数|字段|规格|记录', text):
        return False
    if not re.search(r'未知|未(?:记录|登记|写|记载)|没有(?:写|记录)|查不到', text):
        return False
    if not re.search(r'确认方法|核实方法|核对方法|确认流程|核实流程|'
                     r'如何(?:确认|核实|核对)|怎么(?:确认|核实|核对)|怎么办', text):
        return False
    if re.search(r'订单|售后|退货|退款|换货|维修|物流|发票|开票|'
                 r'报价|实付|售价|价格|库存|补货|有货|推荐|选购|购买|下单|'
                 r'想买|要买|我买|我选|想选|帮我选|比较|对比|哪款适合', text):
        return False
    return True


def catalogue_verification_instruction(clause):
    """A future source-checking step does not claim an image was received.

    This narrow predicate supplements the absent-image publication guard. It
    preserves an imperative procedure, never a completed reading/observation,
    and does not bypass the guard for any other clause in the same response.
    """
    if not re.match(r'\s*(?:[-*•]\s*)?'
                    r'(?:先|再|请(?:你|您)?(?:先|再)?|建议(?:你|您)?|'
                    r'需要(?:你|您)?|应当|应该|应)'
                    r'(?:核对|核实|查看|查阅|检查|确认|读取)', clause):
        return False
    if not re.search(r'原(?:始)?(?:商品|产品)?资料|商品资料|产品资料|原文|'
                     r'说明书|标签|铭牌', clause):
        return False
    if re.search(r'已(?:经)?|刚才|刚刚|之前|看(?:到|见|清|出)|读(?:到|出)|'
                 r'写着|写有|标着|标注(?:为)?|显示|可见|辨认出|识别出|'
                 r'核对(?:过|了)|查看(?:过|了)|确认(?:过|了)', clause):
        return False
    return True


def _method_identity_question(scope):
    # A concrete identity/field or original evidence can be a legitimate next
    # step in a verification method. A desired capacity/colour is a preference.
    return bool(re.search(r'型号|商品(?:名称|编号)|具体商品|哪个字段|哪(?:一)?项'
                          r'(?:资料|参数|记录|信息)|需(?:要)?(?:确认|核实|核对|查询)'
                          r'.{0,12}(?:字段|资料|参数)|标签|铭牌|原文|资料来源', scope))


def _followup_request(scope):
    """Customer references in affirmative answers are not interrogatives."""
    scope = re.sub(r'[“「][^”」]*[”」]', '', scope)
    return bool(re.search(
        r'[？?]|(?:吗|么|呢)(?:[。！!；;\s]|$)|'
        r'哪(?:一|种|项|个|款|类|笔|单)|什么|多少|还是|或者|'
        r'请(?:问|(?:你|您)?(?:先|再)?(?:告诉|提供|说|说明|描述|补充|选))|'
        r'(?:麻烦|方便).{0,8}(?:告诉|提供|说|描述|补充)|告诉我|发我|'
        r'(?:也|再|顺便)(?:说|告诉|提供)', scope))


def prune_questions(answer, ctx, question, *, intents, named_products,
                    shopping_requested=False):
    """Remove only unrelated asks for recognized, completed read-only scopes.

    Real after-sale, mixed shopping, unrecognized queries, confirmation, and
    missing-SKU flows remain in their established path. Facts/unknown evidence
    before an ask and independent human-review boundaries remain visible.
    """
    positive = _positive_request(question)
    service_work = any(i.get('kind') == 'after_sale'
                       and i.get('query_subject') != 'refund_basis' for i in intents)
    order_work = service_work or any(i.get('kind') == 'status' for i in intents)
    visual_only = bool(re.search(r'图片|照片|外观|标签|铭牌|参考图|看图', question)) \
        and not order_work and not shopping_requested
    parameter_only = bool(named_products and re.search(
        r'参数|资料|记录|容量|材质|功能|续航|功率|尺寸|洗护|护理|使用|蓝牙|'
        r'包装|清单|颜色|尺码|区别|差别', positive)) \
        and not service_work and not shopping_requested
    missing_category = _current_missing_category(ctx, question, named_products)
    basis_only = any(i.get('query_subject') == 'refund_basis' for i in intents) \
        and not service_work and not shopping_requested
    method_only = catalogue_method_only(question) and not order_work and not shopping_requested
    if not (visual_only or parameter_only or missing_category or basis_only or method_only):
        return answer

    identity_query = bool(re.search(r'哪(?:一)?款|型号|识别|辨认|是什么商品', positive))
    pieces = []
    dropped_ask = False
    # Split independent asks at a comma only within the new method scope. Mask
    # balanced quotations so a quoted bad question is evidence, not an action.
    literals = {}
    if method_only:
        def keep_literal(match):
            token = f'＄确认原文{len(literals)}＄'
            literals[token] = match[0]
            return token
        answer = re.sub(r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', keep_literal, answer)
    split_at = r'(?<=[，,。！？?；;\n])' if method_only else r'(?<=[。！？?；;\n])'
    for part in re.split(split_at, answer):
        if not part.strip():
            pieces.append(part)
            continue
        # These are authority/evidence statements, never optional preferences.
        if re.search(r'确认提交|提交人工|人工.{0,8}(?:终审|审核)|核对.{0,8}(?:申请|草稿|卡)', part):
            pieces.append(part)
            continue
        cue = _ASK.search(part)
        if cue and _CHOICE_FIELDS.search(part[cue.start():]) and _followup_request(part[cue.start():]):
            scope = part[cue.start():]
            if method_only and _method_identity_question(scope):
                pieces.append(part)
                continue
            # A completed catalogue subtask cannot erase the blocking field
            # of a separate current order/status/eligibility subtask.
            if order_work and re.search(r'订单|哪(?:一)?笔|哪(?:一)?单', scope):
                pieces.append(part)
                continue
            if service_work and re.search(
                    r'故障|原因|问题|责任|使用|经过|出现|开箱|摔|进水|破损|'
                    r'包装|配件|退货|退款|换货|维修|签收|收货|物流', scope):
                pieces.append(part)
                continue
            if (any(i.get('request_type') == 'exchange' and i.get('query_subject') != 'refund_basis'
                    for i in intents) and re.search(r'颜色|尺码|规格|型号|哪(?:一)?款', scope)):
                pieces.append(part)
                continue
            # Clearer labels are a valid identification next step; a pure
            # formula may still explain how to locate one's own order.
            if visual_only and identity_query and re.search(r'标签|铭牌|型号', scope) \
                    and not re.search(r'订单|预算|颜色|尺码|用途|功能', scope):
                pieces.append(part)
                continue
            if basis_only and re.search(r'订单|数量|几件|几副', scope) \
                    and not re.search(r'故障|原因|责任|颜色|尺码', scope):
                pieces.append(part)
                continue
            prefix = part[:cue.start()].rstrip('，,；;：: \n')
            if method_only and re.fullmatch(r'(?:也|再|并|另外|此外|同时|顺便|然后)', prefix):
                # Removing an independent optional ask must not leave its
                # connective as an isolated sentence (e.g. "也。" ).
                prefix = ''
            if prefix:
                pieces.append(prefix + ('。' if not re.search(r'[。！？?]$', prefix) else ''))
            dropped_ask = True
            continue
        if (missing_category or dropped_ask) and _OPTIONAL_SALES.search(part):
            continue
        pieces.append(part)
    result = re.sub(r'\n{3,}', '\n\n', ''.join(pieces)).strip()
    for token, literal in literals.items():
        result = result.replace(token, literal)
    return result
