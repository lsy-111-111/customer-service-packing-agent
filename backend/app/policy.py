"""Deterministic after-sale advice; never approves or executes a real case."""
from datetime import datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo
from .facts import values

ENGINE_VERSION = '2026-09-effective-policy-v3.6'
DEFAULTS = {'return_days': 7, 'exchange_days': 15, 'quality_days': 30, 'reship_days': 30, 'warranty_days': 365}
RULE_KEYS = {
    'NR-7': 'no_reason_return', 'EX-15': 'non_quality_exchange',
    'QUALITY-30': 'quality_return_exchange', 'CUSTOMER-DAMAGE': 'customer_damage',
    'NOT-INTACT': 'not_intact', 'RESHIP-MISSING': 'reship_missing',
    'RESHIP-DAMAGE': 'reship_arrival_damage', 'PART-IN-PACKAGE': 'part_in_package',
    'REQUEST-QUANTITY': 'request_quantity', 'NOT-DELIVERED': 'not_delivered',
    'FACTS-INCOMPLETE': 'facts_incomplete', 'MISMATCH': 'request_mismatch',
    'WARRANTY-365': 'warranty', 'QUALITY-REMEDY': 'quality_remedy',
    'MANUAL-REMEDY': 'manual_remedy', 'STOCK-FULFILLMENT': 'stock_fulfillment',
    'EXCHANGE-TARGET': 'exchange_target',
}
_RULE_TEMPLATES = {
    'NR-7': '本次适用 {return_days} 日无理由退货期限：合理查验不直接影响完好，退货通知期限内提出。',
    'EX-15': '本次适用店铺 {exchange_days} 日非质量换货承诺：商品完好，同款等价规格。',
    'QUALITY-30': '本次适用店铺 {quality_days} 日质量退换承诺：非顾客造成的质量问题可退换。',
    'CUSTOMER-DAMAGE': '顾客自行损坏、遗失或使用不当，不属于免费质量退换或补发责任。',
    'NOT-INTACT': '明显使用损耗或人为损坏影响完好，不符合无理由退换条件；合理查验除外。',
    'RESHIP-MISSING': '按订单实际欠发数量补发，不能重复补偿同一缺失；便捷申报期为 {reship_days} 日，超过该期不免除已经确认的欠发责任。',
    'RESHIP-DAMAGE': '到货破损可补发原规格，便捷申报期为 {reship_days} 日；整件执行前须记录回收安排或人工免退。',
    'PART-IN-PACKAGE': '免费补件仅覆盖购买时包装清单中的对应配件和欠缺数量。',
    'REQUEST-QUANTITY': '申请不能超过该商品单位或配件尚可处理的数量。',
    'NOT-DELIVERED': '未签收问题进入物流核实，不能按已收到且完好的商品处理。',
    'FACTS-INCOMPLETE': '关键事实不足时先补充，不推定顾客有过错或商品完好。',
    'MISMATCH': '申请理由与商品不一致时先澄清具体诉求。',
    'WARRANTY-365': '数码、家电本次适用店铺 {warranty_days} 日保修；超过 {quality_days} 日直接质量退换承诺期后，继续核实维修及其他法定救济。',
    'QUALITY-REMEDY': '有效保障内非人为质量问题且维修不能解决，可继续申请换货或退货；超过已确认保障须核实其他法定依据，不因日期一概拒绝，也不承诺无限期退换。',
    'MANUAL-REMEDY': '仅退款、维修和物流争议由人工接续，系统不虚构执行结果。',
    'STOCK-FULFILLMENT': '缺货仅影响履约安排，不影响已成立的售后资格。',
    'EXCHANGE-TARGET': '当前闭环支持同款等价规格；跨款或差价需人工方案。',
}
# Legacy IDs remain readable for saved records. New explanations must use
# rule_titles(..., effective_policy(...)), never this default-policy view.
RULES = {key: template.format(**DEFAULTS) for key, template in _RULE_TEMPLATES.items()}


def policy_context(rules, *, order_policy_version=None):
    """Canonical provenance for one evaluation, including the executing engine.

    Flat parameters are authoritative for compatibility with existing callers.
    Recompute instead of trusting a fingerprint supplied by a saved snapshot.
    """
    params = {key: rules.get(key, default) for key, default in DEFAULTS.items()}
    params['no_reason_shipping'] = rules.get('no_reason_shipping', 'customer')
    context = {
        'order_policy_version': (order_policy_version if order_policy_version is not None
                                 else rules.get('order_policy_version', rules['version'])),
        'current_policy_version': rules.get('current_policy_version', rules['version']),
        'params': params,
        'engine_version': ENGINE_VERSION,
    }
    encoded = json.dumps(context, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    context['fingerprint'] = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    return context


def policy_fingerprint(rules, *, order_policy_version=None):
    return policy_context(rules, order_policy_version=order_policy_version)['fingerprint']


def rule_titles(rule_ids_or_keys, rules):
    """Render semantic or legacy rule identifiers against actual policy terms."""
    legacy_ids = {value: key for key, value in RULE_KEYS.items()}
    params = {key: rules.get(key, default) for key, default in DEFAULTS.items()}
    return [_RULE_TEMPLATES[legacy_ids.get(key, key)].format(**params)
            for key in rule_ids_or_keys]


def effective_policy(original, current):
    result = dict(original)
    for key in DEFAULTS:
        result[key] = max(original.get(key, 0), current.get(key, DEFAULTS[key]))
    # An earlier promise of store-paid no-reason shipping cannot be taken away.
    result['no_reason_shipping'] = 'store' if 'store' in (original.get('no_reason_shipping'), current.get('no_reason_shipping')) else 'customer'
    result['order_policy_version'] = original['version']
    result['version'] = current['version']
    result['current_policy_version'] = current['version']
    result.update(policy_context(result))
    return result


def material_proposal(value):
    """The customer-confirmed proposal, excluding explanatory/provenance changes.

    Compare complete cases (or consistently shaped evaluation results). Do not
    compare a partial evaluation directly with a complete case. A rule refresh
    can update explanations without erasing confirmation when this is unchanged.
    """
    assessment = value.get('assessment') or {}
    subject = assessment.get('subject') or {}
    proposal = {key: value.get(key, subject.get(key)) for key in (
        'order_id', 'type', 'quantity', 'target_sku_id', 'reship_kind', 'part_sku_id',
        'refund_cents', 'reason', 'condition_note', 'return_handling', 'return_note')}
    proposal['attachment_ids'] = sorted(value.get('attachment_ids') or [])
    proposal['shipping_payer'] = value.get('shipping_payer', assessment.get('shipping_payer'))
    proposal['decision'] = assessment.get('decision')
    proposal['missing_fields'] = sorted(assessment.get('missing_fields') or [])
    proposal['fulfillment_status'] = (assessment.get('fulfillment') or {}).get('status')
    proposal['facts'] = values(value.get('facts') or assessment.get('facts_used') or {})
    return proposal


def proposal_changed(before, after):
    return material_proposal(before) != material_proposal(after)


def no_reason_eligibility_without_preference(kind, f):
    """A return needs no personal preference, but must not erase a known dispute."""
    return (kind == 'return_refund' and f.get('issue_type', 'unknown') == 'unknown'
            and (f.get('condition') in ('intact', 'inspection_only') or f.get('unopened_return_basis') is True)
            and f.get('responsibility', 'unknown') == 'unknown'
            and not any(f.get(key) is True for key in ('defect_on_arrival', 'normal_use', 'repair_failed', 'causation_conflict'))
            and not f.get('missing_quantity'))


def _reported_abnormality(request, facts):
    """Select a relevant question without inferring defect cause or liability."""
    if facts.get('condition') == 'damaged':
        return True
    text = '\n'.join(str(request.get(key) or '') for key in ('reason', 'condition_note'))
    for clause in re.split(r'[，。；！？\n]', text):
        match = re.search(r'(?:商品|产品|东西|设备|耳机|水壶|杯子|风扇|衣服|它)(?:(?:已经|现在|突然|确实)|(?:正常|按说明(?:书)?|按要求)?使用\s*\d+\s*(?:天|日|个月|年)后)?(?:坏了|坏掉了|破损了|损坏了|无法正常工作|无法开机|不能开机|不能正常使用|不能加热|无法加热)', clause)
        if match and not re.search(r'没(?:有)?(?:说|表示)|不是(?:说)?|并非|如果|假如|假设|担心', clause[:match.start()]):
            return True
    return False


def assess(order, product, request, rules, facts, *, now, target=None, part=None, available_quantity=None):
    context = policy_context(rules, order_policy_version=order['policy_version'])
    rules = {**rules, **context['params']}
    f = values(facts)
    kind = request.get('type')
    qty = request.get('quantity', 1)
    issue = f.get('issue_type', 'unknown')
    condition = f.get('condition', 'unknown')
    responsibility = f.get('responsibility', 'unknown')
    unopened_basis = (kind == 'return_refund' and f.get('unopened_return_basis') is True
                      and f.get('normal_use') is not True and responsibility != 'customer')
    time = request.get('first_notice_at') or now
    def date(value):
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).date()
    signed = date(order['delivered_at']) if order.get('delivered_at') else None
    notice = date(time)
    elapsed = (notice - signed).days if signed else None
    result = {'decision': 'needs_info', 'headline': '请先补充关键信息', 'reasons': [], 'rule_ids': [], 'rule_keys': [],
              'policy_version': rules['version'], 'order_policy_version': order['policy_version'],
              'policy_fingerprint': context['fingerprint'], 'policy_context': context,
              'engine_version': ENGINE_VERSION, 'facts_used': facts, 'missing_fields': [], 'questions': [],
              'shipping_payer': 'pending', 'fulfillment': {'status': 'ready', 'stock': None, 'required_quantity': qty},
              'next_steps': [], 'evaluated_at': now,
              'subject': {'order_id': order['id'], 'product_id': order.get('product_id'), 'type': kind,
                          'quantity': qty, 'reship_kind': request.get('reship_kind'),
                          'part_sku_id': request.get('part_sku_id'), 'target_sku_id': request.get('target_sku_id')},
              'dates': {'delivered_at': order.get('delivered_at'), 'first_notice_at': time, 'elapsed_days': elapsed}}
    def end(decision, rule, reason, *, missing=None, question=None, payer='pending', manual=False):
        result.update(decision=decision, headline={'approve': '建议同意', 'reject': '建议拒绝本次请求', 'needs_info': '请先补充关键信息'}[decision], shipping_payer=payer)
        result['rule_ids'].append(rule); result['reasons'].append(reason)
        if missing: result['missing_fields'].append(missing)
        if question: result['questions'].append(question)
        if manual: result['fulfillment']['status'] = 'manual'
        result['next_steps'] = ([question] if question else []) + (['请人工结合已核实事实确认后处理。'] if manual else ['核对申请后提交，由人工终审；本建议不代表已经批准、退款或发货。'])
        if decision == 'approve' and kind in ('exchange', 'reship'):
            stock = (part if request.get('reship_kind') == 'part' else target) or {}
            result['fulfillment']['stock'] = stock.get('stock')
            if stock.get('stock', 0) < qty:
                result['fulfillment']['status'] = 'out_of_stock'
                result['rule_ids'].append('STOCK-FULFILLMENT')
                result['next_steps'].insert(0, '资格成立；当前库存不足，请人工确认补货或协商替代方案，不能承诺已发出。')
        result['rule_keys'] = [RULE_KEYS[r] for r in result['rule_ids']]
        result['policy_basis'] = rule_titles(result['rule_keys'], rules)
        result['rule_titles'] = list(result['policy_basis'])
        return result
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1 or (available_quantity is not None and qty > available_quantity):
        return end('reject', 'REQUEST-QUANTITY', '申请数量超过尚可处理的数量；不能重复补偿同一商品或配件。')
    if f.get('causation_conflict') is True and kind in ('return_refund','exchange','reship','repair','refund_only'):
        return end('needs_info', 'FACTS-INCOMPLETE', '关于开箱状态、后续外力及故障先后的陈述仍有冲突，不能仅凭先前“到货就坏”或正常使用的说法确定责任。', missing='responsibility', question='请确认故障是在摔碰等事件之前还是之后出现；如果目前无法确定，可由人工结合材料核实，暂不推定责任。', manual=True)
    if kind in ('return_refund','exchange','repair') and 'received_quantity' in f and f['received_quantity'] < qty:
        return end('needs_info', 'FACTS-INCOMPLETE', '你说明的实际收货数量少于本次整件退货、换货或维修数量，需要先核实，不能仅按系统签收或商品完好陈述认定已收到。', missing='received_quantity', question='请确认本订单实际收到几件、希望退换或维修其中几件；如果说的是缺少配件或未收到货，可分别核对补件或物流处理。', manual=True)
    if kind == 'refund_only':
        if order.get('status') in ('unshipped','paid','pending_shipment'):
            return end('approve', 'MANUAL-REMEDY', '订单尚未发货，建议同意取消并按订单实付退款，由人工接续操作。', manual=True, payer='store')
        if order.get('status')=='delivered' and issue=='no_reason' and condition in ('intact','inspection_only'):
            return end('reject', 'MANUAL-REMEDY', '已收到且完好的商品无免退依据，不能既保留商品又要求仅退款；可按期限申请退货退款。', manual=True, payer='customer')
        if responsibility=='customer':
            return end('reject', 'CUSTOMER-DAMAGE', '顾客自行造成的问题不符合免费免退退款条件；可由人工提供其他处理方案。', manual=True, payer='customer')
        return end('needs_info', 'MANUAL-REMEDY', '仅退款需要核实是否已发货、是否实际收货及免退依据。', missing='refund_basis', question='商品是否已经收到，为什么需要无需退回直接退款？', manual=True)
    if kind == 'logistics':
        known_shortage = (issue == 'missing' and type(f.get('received_quantity')) is int
                          and 0 <= f['received_quantity'] < order.get('quantity', 0))
        reported_arrival_damage = issue == 'damage' and f.get('defect_on_arrival') is True
        if (order.get('status') in ('shipping','shipped') or f.get('received_quantity')==0
                or known_shortage or reported_arrival_damage):
            return end('approve', 'MANUAL-REMEDY', '建议同意受理物流调查，由人工核查配送与实际签收；调查受理不代表已经退款或补发。', manual=True)
        if 'received_quantity' in f:
            return end('needs_info', 'MANUAL-REMEDY', '已记录顾客陈述的实收数量，还需明确希望调查的物流争议。', missing='logistics_issue', question=f"你说明实际收到 {f['received_quantity']} 件。还希望人工调查哪项物流问题？", manual=True)
        return end('needs_info', 'MANUAL-REMEDY', '需要确认物流争议的具体事实。', missing='received_quantity', question='是没有收到、签收异常，还是配送破损？请说明实际收货情况。', manual=True)
    if kind == 'repair':
        if responsibility=='customer':
            return end('reject', 'CUSTOMER-DAMAGE', '已明确的人为损坏不属于免费保修，可由人工沟通付费维修可行性。', manual=True, payer='customer')
        if order.get('status') != 'delivered' or not signed or elapsed < 0:
            return end('needs_info', 'NOT-DELIVERED', '目前还不能确认实际收货及报修时间，不能据此判断保修期限。', missing='received_status', question='请核对实际收货和首次报修时间；未收货可由人工接续核查物流。', manual=True)
        has_warranty = product.get('category') in ('数码', '家电')
        boundary = (f"首次申请距签收 {elapsed} 日，本次适用的店铺保修期限为 {rules['warranty_days']} 日。"
                    if has_warranty else '当前已确认的店铺数码家电保修承诺不直接适用于本商品类别。')
        if has_warranty and elapsed > rules['warranty_days']:
            return end('needs_info', 'WARRANTY-365', boundary + '已超过当前资料确认的店铺保修期，暂不能承诺免费维修；仍需核实更长保障、期内报修记录或其他有效救济，不能只按日期排除全部权利。', missing='additional_remedy_basis', question='是否有更长保障承诺、此前保修期内的报修记录或其他维修保障依据？可由人工结合已有故障及使用陈述继续核实维修方案。', manual=True)
        if has_warranty and issue in ('quality','damage') and (responsibility=='seller' or f.get('normal_use') is True or f.get('defect_on_arrival') is True):
            return end('approve', 'WARRANTY-365', f"数码家电在本次适用的 {rules['warranty_days']} 日店铺保修期内，所述故障属非人为质量问题，建议同意免费保修并由人工接续。", manual=True, payer='store')
        questions = []
        if issue not in ('quality', 'damage') and not _reported_abnormality(request, f):
            questions.append('请补充当前需要维修的具体异常表现。')
        if responsibility == 'unknown' and 'normal_use' not in f and f.get('defect_on_arrival') is not True:
            questions.append('请补充故障是在开箱时还是使用中发现，是否发生过摔碰、进水或改装。')
        questions.append('由人工结合已有陈述核实故障原因、适用维修保障与方案。')
        return end('needs_info', 'WARRANTY-365' if has_warranty else 'MANUAL-REMEDY', boundary + '仍需核实故障原因及保障范围，暂不承诺免费维修。', missing='repair_basis', question=''.join(questions), manual=True)
    if order.get('status') != 'delivered' or not signed:
        return end('needs_info', 'NOT-DELIVERED', '订单尚未签收，请先核实物流及实际收货情况。', missing='received_status', question='请由人工结合已提供说明，核实本订单当前物流及实际签收情况后接续处理。', manual=True)
    if elapsed < 0:
        return end('needs_info', 'FACTS-INCOMPLETE', '申请通知早于签收日期，需要核实实际收货和请求时间。', missing='received_status', question='请核对是否已实际收货；未收货问题应由人工按物流情况处理。', manual=True)
    if product.get('category') != '服饰' and any(word in request.get('reason', '') for word in ('尺码', '穿着', '穿不上')) and not f.get('mismatch_resolved'):
        return end('needs_info', 'MISMATCH', '这笔订单不是服饰，“尺码”原因需要核对，不能据此认定商品质量问题。', missing='mismatch_resolved', question='你指的是容量或机身尺寸不合适，还是发错商品／与介绍不符？')
    if kind=='reship' and issue in ('unknown','missing','no_reason') and f.get('missing_quantity')==0:
        return end('reject', 'RESHIP-MISSING', '已明确没有欠发数量，不能把额外赠送作为本订单免费补发；如有其他质量或破损问题，可另行核实对应处理。', payer='customer')
    # A later explicit self-damage correction is the governing reason even if
    # an earlier whole-item reship request was misclassified as a part request.
    # Otherwise PART-IN-PACKAGE would give the correct decision with a false
    # explanation that the purchased cup was an unlisted accessory.
    if responsibility == 'customer' and issue != 'no_reason':
        return end('reject', 'CUSTOMER-DAMAGE', '根据你提供的事实，问题由自行损坏、遗失或使用不当造成，不符合本次免费售后条件。', payer='customer')
    if kind == 'reship' and request.get('reship_kind') == 'part':
        if not request.get('part_sku_id'):
            return end('needs_info', 'FACTS-INCOMPLETE', '需要确认缺少或破损的具体配件。', missing='part_sku_id', question='包装中具体少了或损坏了哪个配件？')
        if part is None:
            return end('reject', 'PART-IN-PACKAGE', '购买时包装清单未包含该配件，不能作为本订单免费补发项。')
    if issue=='no_reason' and kind in ('return_refund','exchange') and condition in ('used','damaged'):
        return end('reject', 'NOT-INTACT', '已说明存在自行改装、明显使用痕迹或损坏，超出合理查验并影响完好，不符合本次无理由退换条件。', payer=rules['no_reason_shipping'])
    if kind == 'exchange':
        if not target:
            if (issue in ('quality','damage','wrong_item','missing')
                    and (responsibility=='seller' or f.get('defect_on_arrival') or f.get('normal_use'))
                    and elapsed <= rules['quality_days']):
                # Missing fulfillment choices must not erase an already
                # assessable quality basis. No target or application is inferred.
                result['eligibility_note'] = (f"根据你提供的非人为质量或卖家责任陈述，首次申请在本次适用的 {rules['quality_days']} 日质量退换承诺内，建议同意质量换货；具体方案还需确认同款等价规格，并由人工核实审核。")
                return end('needs_info', 'QUALITY-30', result['eligibility_note'], missing='target_sku_id',
                           question='你希望换成哪个同款颜色或尺码？确认后再核对库存和具体方案。', payer='store')
            return end('needs_info', 'FACTS-INCOMPLETE', '需要明确希望换成的同款规格。', missing='target_sku_id', question='你希望换成哪个颜色或尺码？')
        if target.get('product_id') != order['product_id'] or target.get('price_cents') != request.get('original_price_cents', target.get('price_cents')):
            return end('reject', 'EXCHANGE-TARGET', '当前请求不是同款等价规格，需人工沟通差价或跨款方案。', manual=True)
    # No-reason return eligibility does not require the customer to disclose a
    # preference. Keep the facts unknown: this is a rule route, not extraction.
    reported_abnormality = issue == 'unknown' and _reported_abnormality(request, f)
    no_preference_required = (not reported_abnormality and no_reason_eligibility_without_preference(kind, f)
                              and elapsed <= rules['return_days'])
    if issue == 'unknown' and not no_preference_required:
        if reported_abnormality:
            # An omitted extraction does not mean the customer never supplied
            # the observations. Preserve their words as a claim, not a finding
            # of liability, and let staff use that material without asking for
            # the entire history again. This does not change eligibility.
            statement = str(request.get('reason') or '').strip()
            excerpt = statement[:600] + ('…' if len(statement) > 600 else '')
            reason = '已收到你关于商品情况的陈述，现有陈述尚未完成核验，暂不能确认本订单的实际故障、直接退换资格或责任。'
            if excerpt:
                reason += f'已收到的顾客原话（待核验）：“{excerpt}”。'
            details_supplied = re.search(r'正常使用|按(?:照)?说明|无摔碰|没(?:有)?摔|(?:不能|无法)(?:正常工作|开机|充电|出声|工作|加热)|\d+\s*(?:天|日|个月|年)后', statement)
            question = ('已提供的相关陈述会一并保留，可由人工根据这些原话继续核实；如有尚未提供的检测、保障或责任证明材料，可以补充，不需要重复提交相同说明。'
                        if details_supplied else
                        '如本订单确有异常，请补充尚未说明的具体表现、首次发现时间、使用情况、摔碰或进水经历；已经提供的说明无需重复，不清楚的部分可由人工核实。')
            return end('needs_info', 'FACTS-INCOMPLETE', reason, missing='responsibility', question=question, manual=True)
        return end('needs_info', 'FACTS-INCOMPLETE',
                   '尚不清楚这是个人偏好、质量问题、破损还是少发；如涉及故障或破损，发生经过与责任也待核实，不能预先归责。',
                   missing='issue_type',
                   question='请一并说明具体问题和出现经过：是不合适、故障、破损还是少发；如有故障或破损，首次发现时是否正常使用，有无摔碰或进水？')
    if kind == 'reship':
        if issue not in ('missing', 'damage', 'wrong_item', 'quality'):
            return end('reject', 'RESHIP-MISSING', '商品没有少发、到货破损或卖家责任问题，个人偏好不符合免费补发条件。', payer='customer')
        # The delivered order is the system-side signature evidence. A
        # normalized zero-receipt fact may arrive with split, colloquial or
        # absent quote metadata; none verifies that the seller caused the
        # shortfall. Route the whole-order dispute to logistics review.
        if (issue == 'missing' and request.get('reship_kind') != 'part'
                and f.get('received_quantity') == 0 and order.get('status') == 'delivered'
                and responsibility != 'seller'):
            return end('needs_info', 'FACTS-INCOMPLETE',
                       '系统显示签收、顾客陈述整单未收到，尚需核查物流签收与实际交付；不能仅凭实收零件直接同意整件补发。',
                       missing='delivery_dispute',
                       question='请由人工核对物流签收凭证、配送及实际收货；若核实确属卖家欠发，再评估补发。',
                       manual=True)
        if issue == 'wrong_item':
            return end('reject', 'EXCHANGE-TARGET', '发错商品应安排退换；需回收已收到的错发商品，不能直接额外免费补发。', manual=True, payer='store')
        if issue == 'quality' and not f.get('defect_on_arrival'):
            return end('needs_info', 'QUALITY-REMEDY', '使用中的质量故障需要核实换货、保修及原件回收安排，不能直接按到货欠损补发。', missing='repair_failed', question='是否需要退回换货，或已维修仍无法解决？请说明故障和维修情况。', manual=True)
        if issue == 'missing' and 'missing_quantity' not in f and 'received_quantity' not in f:
            return end('needs_info', 'FACTS-INCOMPLETE', '补发数量须与实际欠发数量一致。', missing='missing_quantity', question='应收到多少、实际收到多少，具体少了几件？')
        confirmed_missing=(issue=='missing' and ('missing_quantity' in f or 'received_quantity' in f) and f.get('defect_on_arrival') is not False)
        if responsibility != 'seller' and not f.get('defect_on_arrival') and not confirmed_missing:
            return end('needs_info', 'FACTS-INCOMPLETE', '需要区分到货缺损和后续使用造成的问题。', missing='responsibility', question='问题是在收到／开箱时就存在，还是使用过程中发生的？')
        missing_qty = f.get('missing_quantity')
        expected=request.get('expected_component_quantity',order['quantity'])
        if issue=='missing' and 'received_quantity' in f and (f['received_quantity']>expected or (missing_qty is not None and f['received_quantity']+missing_qty!=expected)):
            return end('needs_info', 'FACTS-INCOMPLETE', '实收和欠发数量与订单应含数量不一致，需要先核对，不能据此多补。', missing='received_quantity', question='请核对本订单应收、实际收到和缺少的数量，是否把不同订单或其他配件合在一起了？')
        if missing_qty is None and 'received_quantity' in f:
            missing_qty = expected - f['received_quantity']
        if issue == 'missing' and (missing_qty is not None and qty > missing_qty):
            return end('reject', 'REQUEST-QUANTITY', '本次补发数量超过你说明的实际欠发数量。')
        reason = '根据已提供的到货缺损事实与数量，建议同意补发。'
        if elapsed > rules['reship_days']:
            reason = (f"根据已提供的收货事实，符合原规格补发条件；超过 {rules['reship_days']} 日便捷申报期"
                      + ('也不免除已经确认的欠发责任。' if issue == 'missing' else '仍按已确认的到货破损或卖家责任事实处理。'))
        return end('approve', 'RESHIP-MISSING' if issue == 'missing' else 'RESHIP-DAMAGE', reason, payer='store')
    quality = issue in ('quality', 'damage', 'wrong_item', 'missing')
    if quality:
        if responsibility != 'seller' and not f.get('defect_on_arrival') and not f.get('normal_use'):
            return end('needs_info', 'FACTS-INCOMPLETE', '商品异常与责任原因尚需区分，不能仅凭照片或故障词判断由谁承担。', missing='responsibility', question='是开箱时就发现的问题，还是按说明正常使用后出现？有没有摔碰、进水或改装？')
        if elapsed <= rules['quality_days']:
            return end('approve', 'QUALITY-30', f"首次申请在签收次日起 {rules['quality_days']} 日质量退换承诺内，所述情况属于非人为质量或卖家责任问题。", payer='store')
        guaranteed_days=rules['warranty_days'] if product.get('category') in ('数码','家电') else rules['quality_days']
        if elapsed > guaranteed_days:
            return end('needs_info', 'QUALITY-REMEDY', f"首次申请距签收 {elapsed} 日，已超过本次适用的 {rules['quality_days']} 日直接质量退换承诺及 {guaranteed_days} 日店铺保障期，需要核实其他有效保障或法定退换依据；不能只因日期拒绝，也不能仅凭维修失败无限期承诺退换。", missing='additional_remedy_basis', question='是否有更长保障承诺、此前期限内报修记录或其他法定退换依据？请交由人工进一步核实。', manual=True)
        if f.get('repair_failed') is True:
            return end('approve', 'QUALITY-REMEDY', f"正常使用的质量问题经维修仍不能解决，继续支持退换救济，不因超过 {rules['quality_days']} 日直接质量退换承诺期自动拒绝。", payer='store')
        if 'repair_failed' not in f:
            return end('needs_info', 'WARRANTY-365' if product.get('category') in ('数码', '家电') and elapsed <= rules['warranty_days'] else 'QUALITY-REMEDY', f"超过本次适用的店铺 {rules['quality_days']} 日直接质量退换期，仍需核实维修、故障及其他退换依据。", missing='repair_failed', question='是否已经维修仍无法正常使用，或有其他退换依据？可先由人工核实保修与质量救济。', manual=True)
        return end('reject', 'WARRANTY-365' if product.get('category') in ('数码', '家电') and elapsed <= rules['warranty_days'] else 'QUALITY-REMEDY', '目前事实为尚未维修且未说明其他直接退换依据，因此不建议直接退换；继续由人工处理保修、维修及可能适用的法定救济。', manual=True, payer='pending')
    days = rules['exchange_days'] if kind == 'exchange' else rules['return_days']
    rule = 'EX-15' if kind == 'exchange' else 'NR-7'
    if elapsed > days:
        return end('reject', rule, f"首次申请已超过签收次日起 {days} 日非质量{'换货' if kind == 'exchange' else '退货'}期限；所述情况没有质量或卖家责任问题。", payer=rules['no_reason_shipping'])
    if condition in ('used', 'damaged'):
        return end('reject', 'NOT-INTACT', '已说明存在超出合理查验的使用或损坏，影响商品完好，不符合本次无理由退换条件。', payer=rules['no_reason_shipping'])
    if condition not in ('intact', 'inspection_only') and not unopened_basis:
        return end('needs_info', 'FACTS-INCOMPLETE', f'申请时间在 {days} 日期限内，还需确认商品本体是否完好。', missing='condition', question='商品本体目前是否有污损、破损或其他影响完好的情况？', payer=rules['no_reason_shipping'])
    if f.get('accessories_complete') is False:
        return end('needs_info', 'FACTS-INCOMPLETE', '配件不全需要确认可否补齐或由人工协商，不随意编造扣款。', missing='accessories_resolution', question='具体缺少哪个配件，是否可以一并补齐退回？', payer=rules['no_reason_shipping'])
    reason = (f"首次申请在 {days} 日期限内；据你针对本订单的陈述，商品未拆封未使用、配件包装齐全且没有质量问题，建议按非质量退货处理，仍须核对申请并由人工终审。"
              if unopened_basis and condition not in ('intact', 'inspection_only')
              else f"首次申请在 {days} 日期限内，商品完好或仅合理查验，符合非质量{'换货' if kind == 'exchange' else '退货'}条件。")
    if no_preference_required:
        reason += '无理由退货无需说明个人偏好；本次按该资格给出建议，未推定顾客声明过具体退货理由。'
    return end('approve', rule, reason, payer=rules['no_reason_shipping'])
