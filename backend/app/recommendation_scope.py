"""Keep unsolicited recommendations within the current customer request.

This is a publication guard, not a catalogue rewrite.  It uses the fresh
catalogue, customer-sourced constraint projection and current, owner-checked
quote facts.  Explicit comparisons, quotes and order facts stay visible even
when the requested product is unsuitable.  They do not authorize a purchase
entry for an unsuitable variant.

The agent may supply its existing product/variant/quantity resolvers; importing
the agent here would create a circular dependency.  No database or model calls
are made and the supplied context, cards and facts are never changed.
"""
from copy import deepcopy
import re

from . import card_constraints, conversation_state, shopping_state, request_fragments, recommendation_ownership


_LINK = re.compile(r'\[(?P<label>[^\]\n]+)\]\((?P<path>[^)\s]+)\)'
                   r'|(?P<bare>https?://[^\s<>\]）)。，；！？]+|/products/[^\s<>\]）)。，；！？]+)')
_PATH = re.compile(r'/products/[A-Za-z0-9%._~-]+(?:\?sku=[A-Za-z0-9%._~-]+)?')
_RECOMMEND = re.compile(request_fragments.RECOMMEND
                        + r'|帮我挑|选购|哪款|选一|(?:符合|满足).{0,12}(?:选项|商品)')
_NEGATIVE = re.compile(r'不列入.{0,8}推荐|不推荐|不能推荐|无法推荐|不作为.{0,8}推荐|不符合|不满足|不适合|超(?:出|过)?.{0,8}预算|不在.{0,8}推荐范围|(?:按|根据).{0,12}(?:要求|条件).{0,8}(?:排除|不考虑)|(?:因此|所以).{0,4}(?:排除|不考虑)')
_POSITIVE = re.compile(r'建议选|推荐|可(?:以)?(?:选|买|考虑)|适合|符合.{0,12}(?:要求|条件|预算)')
_SERVICE = re.compile(r'订单|售后|退货|退款|维修|换货|补发|签收|收货|原来买|之前买|以前买|上次买|当时买|历史|原订单|那笔|这笔|原(?:实付|单价|价格|金额)')
_CARRY = re.compile(r'^\s*(?:[-*]|\d+[.、]|它|该款|同款)|库存|有货|缺货|补货|实付|到手价|单价|续航|连接|佩戴')
_CONNECTOR = re.compile(r'^\s*(?:另外|此外|同时|顺便|以及|还有|而且|但|而|和|与|及|再看)[，,\s]*$')


def diversity_request(text):
    """Return only the customer's affirmative multi-category request clause."""
    for _, _, clause in request_fragments.clauses(text):
        match = re.search(r'(?P<count>两|二|2|三|3|四|4)款(?:来自)?'
                          r'(?:不同(?:的)?(?:品类|类别)|跨(?:品类|类别))', clause)
        if (match and re.search(r'推荐|挑|选|给', clause)
                and not re.search(request_fragments.UNASSERTED + r'|如果|假如|要是|倘若', clause)
                and not re.search(request_fragments.NEGATIVE + r'|不(?!同).{0,4}推荐', clause)):
            return {'count': {'两': 2, '二': 2, '三': 3, '四': 4}.get(match['count'],
                             int(match['count']) if match['count'].isdigit() else 0), 'clause': clause}
    return None


def _visible(text):
    return _LINK.sub(lambda m: m.group('label') or '', text)


def _named(products, text):
    return [p for p in products if p.get('id') in conversation_state._products(text, products)]


def _variants(ctx, product, text):
    """Fallback selector using catalogue values and existing colour aliases."""
    options = list(product.get('skus', []))
    explicit = [s for s in options if s.get('id') and re.search(
        r'(?<![A-Za-z0-9_-])' + re.escape(s['id']) + r'(?![A-Za-z0-9_-])', text, re.I)]
    if explicit:
        return explicit
    colors = _colors(product, text)
    sizes = {s.get('size') for s in options if s.get('size') and re.search(
        r'(?<![A-Za-z0-9])' + re.escape(str(s['size'])) + r'(?![A-Za-z0-9])', text, re.I)}
    effective = conversation_state.effective_items({'items': card_constraints._source_items(ctx)}, product)
    if not colors:
        colors = {shopping_state.color_key(value) for i in effective if i.get('key') == 'colors'
                  for value in i.get('value', [])}
    if not sizes:
        sizes = {value for i in effective if i.get('key') == 'sizes' for value in i.get('value', [])}
    if colors:
        options = [s for s in options if shopping_state.color_key(s.get('color')) in colors]
    if sizes:
        options = [s for s in options if s.get('size') in sizes]
    return options


def _colors(product, text):
    aliases = {'白': ('白色', '暖白', '米白'), '黑': ('黑色', '曜石黑', '曜黑'),
               '米': ('米色', '燕麦色'), '绿': ('绿色', '鼠尾草绿', '雾绿')}
    found = set()
    for sku in product.get('skus', []):
        color = sku.get('color')
        if not color:
            continue
        key = shopping_state.color_key(color)
        if any(value in text for value in (*aliases.get(key, ()), color)):
            found.add(key)
    return found


def _has_variant(product, text):
    return bool(_colors(product, text) or any(
        s.get('id') and re.search(r'(?<![A-Za-z0-9_-])' + re.escape(s['id']) + r'(?![A-Za-z0-9_-])', text, re.I)
        or s.get('size') and re.search(r'(?<![A-Za-z0-9])' + re.escape(str(s['size'])) + r'(?![A-Za-z0-9])', text, re.I)
        for s in product.get('skus', [])))


def _catalogue_observations(ctx, facts):
    """Project ordered current tool observations without modifying their source.

    A complete product replaces the initial observation.  An inventory result
    missing a field invalidates that field; a partial quote only contributes
    fields it actually returns.  Cards never contribute inventory authority.
    """
    products = {p['id']: deepcopy(p) for p in ctx.get('products', [])}
    skus = {s['id']: s for p in products.values() for s in p.get('skus', [])}
    for fact in facts:
        tool, value = fact.get('tool'), fact.get('result')
        if not isinstance(value, dict) or value.get('error'):
            continue
        if tool == 'get_product' and value.get('id') in products:
            product = deepcopy(value)
            if 'specs' in product and product['specs'] is None:
                product['specs'] = {}
            products[product['id']] = product
            skus = {s['id']: s for p in products.values() for s in p.get('skus', [])}
        elif tool in ('get_inventory', 'quote_price'):
            sku = skus.get(value.get('sku_id') or value.get('id'))
            if sku is None:
                continue
            for key in ('stock', 'restock_at'):
                if key in value:
                    sku[key] = deepcopy(value[key])
                elif tool == 'get_inventory':
                    sku.pop(key, None)
    return {**ctx, 'products': list(products.values())}


def _quote_results(cards, facts, version):
    # Unannotated writer cards cannot establish a budget pass.  Published cards
    # may contribute their already audited quote, at the same preference version.
    quotes = [c.get('data') for c in cards if c.get('type') == 'quote'
               and (c.get('selection') or {}).get('quote_verified') is True
               and c['selection'].get('preference_version') == version]
    # Current tool order has priority over a previously published card's quote.
    quotes += [f.get('result') for f in facts if f.get('tool') == 'quote_price']
    return [q for q in quotes if isinstance(q, dict) and not q.get('error')
            and q.get('sku_id') and type(q.get('quantity')) is int and q['quantity'] > 0
            and type(q.get('total_cents')) is int and q['total_cents'] >= 0]


def _information_requests(products, text, find_products):
    """A chosen purchase does not cancel a separately requested category.

    Resolve only the current affirmative information clause against actual
    catalogue names/aliases/categories. A broad scope such as electronics
    cannot supply an absent keyboard, and no quantity/choice is created here.
    """
    result = {}
    text = re.sub(r'[“「][^”」]*[”」]', '', text)
    for _, _, clause in request_fragments.clauses(text):
        if (not re.search(r'看看|看一下|看下|了解|介绍|比较|对比|说说', clause)
                or _SERVICE.search(clause) or re.search(request_fragments.UNASSERTED + r'|如果|假如|若', clause)
                or re.search(r'(?:不|别|无需|不用|不要).{0,6}(?:看|了解|介绍|比较|对比|说)', clause)
                or re.search(r'决定买|确定买|要买|想买|选购|购买', clause)):
            continue
        named = {p['id'] for p in find_products(products, clause)}
        categories = set(re.findall(shopping_state.CATEGORIES, clause))
        for product in products:
            labels = [product.get('name', ''), product.get('category', ''), *product.get('aliases', [])]
            if (product['id'] in named or any(category and category in label for category in categories for label in labels)
                    or product.get('category') and product['category'] in clause):
                result.setdefault(product['id'], []).append(clause)
    return {pid: '；'.join(clauses) for pid, clauses in result.items()}


def _current_constraint_update(ctx, text, items):
    """A newly sourced shopping condition applies without a recommend verb.

    Historical preferences and a mere colour/price lookup do not create this
    operation. Require an actual current customer assertion already accepted
    by the state layer; this guard neither extracts nor persists preferences.
    """
    latest = next((m for m in reversed(ctx.get('messages', [])) if m.get('role') == 'user'), {})
    if not latest.get('id') or not any(i.get('source_message_id') == latest['id'] for i in items):
        return False
    # A prior-clause historical budget must not veto a current correction in
    # the next clause. The state layer has already accepted the latter source.
    setting = any(not re.search(request_fragments.UNASSERTED + r'|如果|假如|假设|要是', clause)
        and re.search(r'预算|(?:颜色|尺码|尺寸|功能|条件).{0,8}(?:改|调整|不变|保持|仍)'
                      r'|只要|仅要|必须|(?:现在|这次).{0,10}(?:改|调整|提高|降低|增加)', clause)
        for _, _, clause in request_fragments.clauses(text))
    information = re.search(r'只(?:介绍|查询|看|了解)|(?:介绍|说说|比较|对比|想知道|想了解).{0,15}(?:参数|功能|续航|材质|容量)', text)
    return bool(setting and not information)


class RecommendationScope:
    def __init__(self, ctx, question, shopping_question, cards, facts,
                 find_products, resolve_skus, sku_requests, callback_product_ids=()):
        shopping_question = shopping_state.current_request_text(shopping_question)
        ctx = _catalogue_observations(ctx, facts)
        self.ctx = ctx
        self.products = {p['id']: p for p in ctx.get('products', [])}
        self.find_products = find_products or _named
        self.resolve_skus = resolve_skus or _variants
        self.sku_requests = sku_requests
        self.shopping_question = shopping_question
        self.items = card_constraints._source_items(ctx)
        diverse = diversity_request(shopping_question)
        if diverse:
            # A named exclusion is about the exact IDs, including IDs in
            # different categories, rather than the category of the first ID.
            self.items = [{**item, 'scope': '会话'} if item.get('key') == 'excluded_products' else item
                          for item in self.items]
        # The caller resolves topic callbacks from original customer messages.
        # Unknown IDs never establish a product or override a named request.
        self.callback_products = set(callback_product_ids or ()) & self.products.keys()
        declared = shopping_state.update({}, shopping_question, list(self.products.values()),
                                         'current', conversation_state._products)
        chosen = {s['product_id'] for s in declared.get('selections', [])
                  if s.get('selection_kind') == 'explicit_choice'}
        recall = shopping_state.request(ctx.get('preferences', {}), shopping_question,
            products=list(self.products.values()),
            product_ids=[p['id'] for p in self.find_products(list(self.products.values()), shopping_question)])
        if recall and recall.get('kind') == 'selections':
            users = {m.get('id') for m in ctx.get('messages', []) if m.get('role') == 'user'}
            users.update(ctx.get('customer_edit_sources', []))
            chosen.update(s['product_id'] for s in recall.get('items', []) if s.get('source_message_id') in users)
        recommendation = bool(_RECOMMEND.search(shopping_question))
        purchase = bool(re.search(r'决定买|确定买|想买|要买|改买|我选|我要|我想要|购买', shopping_question))
        informational = bool(re.search(r'参数|功能|保温|续航|容量|材质|规格|介绍|想知道|想了解', shopping_question))
        constraint_update = _current_constraint_update(ctx, shopping_question, self.items)
        # Switching the information topic ("换成X，想知道保温功能")
        # does not authorize removing the comparison's catalogue facts.
        self.active = bool(shopping_question and (recommendation or chosen or constraint_update or self.callback_products)
                           and not (informational and not recommendation and not purchase
                                    and not constraint_update and not self.callback_products))
        # A current category goal authorizes verified catalogue entries.
        # It need not rewrite factual rejection explanations in the prose.
        self.entry_active = self.active or bool(declared.get('goal'))
        self.reference_query = bool(re.search(r'价格|报价|多少钱|实付|到手价|库存|有货|缺货|补货', shopping_question)
                                    and not recommendation and not purchase)
        # A mixed request can ask for a selected item and recommendations too.
        self.chosen = chosen if not recommendation else set()
        self.explicit = {p['id'] for p in self.find_products(list(self.products.values()), shopping_question)}
        self.explicit.update(self.callback_products)
        information = {pid: clause for pid, clause in _information_requests(
            list(self.products.values()), shopping_question, self.find_products).items() if pid not in self.explicit}
        self.explicit.update(information)
        # A source-validated return to the actual selection is an explicit
        # information request even when the current text contains no model.
        if recall and recall.get('kind') == 'selections':
            self.explicit.update(chosen)
        for clause in re.split(r'[，,。；;！？?\n]', shopping_question):
            if (re.search(r'不要|不考虑|不选|排除|别推荐|不推荐', clause)
                    and not re.search(r'比较|对比|价格|报价|多少钱|库存|为什么|原因', clause)):
                self.explicit.difference_update(self._ids(clause))
        self.requested = {pid: {s['id'] for s in self.resolve_skus(ctx, p, information.get(pid, shopping_question))}
                          for pid, p in self.products.items() if pid in self.explicit}
        self.order_products = {o.get('product_id') for o in ctx.get('orders', [])
                               if o.get('id') and re.search(r'(?<![A-Za-z0-9_-])' + re.escape(str(o['id']))
                                                           + r'(?![A-Za-z0-9_-])', question)}
        self.service_products = set(self.order_products)
        for clause in re.split(r'[，,。；;！？?\n]|另外|顺便|同时', question):
            if _SERVICE.search(clause):
                self.service_products.update(self._ids(clause))
        # Replace only the most recent customer text for category relevance;
        # the accompanying order lookup is not an invitation to upsell its item.
        shopping_ctx = dict(ctx)
        messages = list(ctx.get('messages', []))
        index = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get('role') == 'user'), None)
        if index is not None:
            relevance_text = shopping_question
            if diverse:
                # A separate named information request is visible alongside
                # the shortlist, but cannot narrow the shortlist's category.
                # Use the shared intent source spans, not answer vocabulary.
                information_clauses = {item['text'].strip(' \t，,。；;！？?\n') for item in
                    conversation_state.detect_intents(shopping_question, list(self.products.values()))
                    if item.get('kind') in ('question', 'price', 'stock', 'compare')
                    and item.get('product_ids') and not _RECOMMEND.search(item['text'])}
                relevance_text = '；'.join(clause for _, _, clause in request_fragments.clauses(shopping_question)
                    if clause.strip(' \t，,。；;！？?\n') not in information_clauses
                    and not (self.find_products(list(self.products.values()), clause)
                            and re.search(r'不要|不考虑|不选|排除|别推荐|不推荐', clause)))
            messages[index] = {**messages[index], 'content': relevance_text}
        shopping_ctx['messages'] = messages
        relevant, has_scope = card_constraints._relevant_products(shopping_ctx, self.items)
        if self.callback_products:
            # A recalled information topic constrains this publication, while
            # any separately requested current product/category is still kept.
            relevant, has_scope = self.callback_products | self.explicit, True
        self.relevant, self.has_scope = relevant, has_scope
        quotes = _quote_results(cards, facts, ctx.get('preferences', {}).get('version'))
        # Rebuilding customer-visible current money claims needs this turn's
        # tools. An earlier audited card remains a card, never a new quote.
        self.current_quotes = _quote_results([], facts, ctx.get('preferences', {}).get('version'))
        self.quantities = {}
        self.eligible = {}
        self.paths = {}
        self.reasons = {}
        for pid, product in self.products.items():
            for sku in product.get('skus', []):
                path = sku.get('purchase_path')
                if isinstance(path, str) and _PATH.fullmatch(path):
                    self.paths[path] = (pid, sku['id'])
            path = product.get('purchase_path')
            if isinstance(path, str) and _PATH.fullmatch(path):
                self.paths[path] = (pid, None)
            eligible, reasons = set(), []
            quantities = {s['id']: quantity for s, quantity in sku_requests(ctx, product, shopping_question)} if sku_requests else {}
            options = self.resolve_skus(ctx, product, shopping_question)
            # A colour-specific price question is not an exclusive shopping
            # preference. A same-product supplementary offer can use a current
            # quote at the already known requested quantity; it still has to
            # pass every customer-sourced condition below. Never infer a split
            # quantity, revive a previous card, or broaden an explicit choice.
            basis = set(quantities.values()) if sku_requests else {1}
            exclusive_lookup = re.search(r'(?:只|仅|单独)(?:查|查询|看|报价|了解)', shopping_question)
            if (self.active and not self.chosen and not exclusive_lookup
                    and len(basis) == 1 and type(next(iter(basis))) is int
                    and next(iter(basis)) > 0):
                quantity = next(iter(basis))
                verified = {q['sku_id'] for q in self.current_quotes if q['quantity'] == quantity}
                selected = {s['id'] for s in options}
                for sku in product.get('skus', []):
                    if sku['id'] not in selected and sku['id'] in verified:
                        options.append(sku)
                        quantities[sku['id']] = quantity
            self.quantities.update(quantities)
            for sku in options:
                quantity = quantities.get(sku['id'], 1) if not sku_requests else quantities.get(sku['id'])
                matching = {q['sku_id']: q for q in quotes if q['sku_id'] == sku['id'] and q['quantity'] == quantity}
                bad, pending = card_constraints._reasons(product, sku, self.items, matching)
                reasons.extend(r['reason'] for r in [*bad, *pending])
                available = type(sku.get('stock')) is int and type(quantity) is int and 0 < quantity <= sku['stock']
                if not bad and not pending and available:
                    eligible.add(sku['id'])
            if (has_scope and pid not in relevant) or (self.chosen and pid not in self.chosen):
                eligible.clear()
            self.eligible[pid] = eligible
            self.reasons[pid] = list(dict.fromkeys(reasons))

    def allows_path(self, path):
        """Whether a current catalogue destination is a conforming entry."""
        if not self.entry_active:
            return True
        record = self.paths.get(path)
        if not record:
            return False
        pid, sid = record
        # A quoted/inventory lookup of the customer's exact chosen variant is
        # still useful when unavailable.  This is an information entry only;
        # the service's published card continues to deny its purchase CTA.
        if self.reference_query and pid in self.explicit:
            return sid is None or sid in self.requested.get(pid, set()) or sid in self.eligible.get(pid, set())
        allowed = self.eligible.get(pid, set())
        if sid:
            return sid in allowed
        # A generic page can expose the product for further clarification;
        # it never establishes an exact SKU.  A product with no currently
        # eligible variant cannot escape that result through its generic page.
        return bool(allowed)

    def filter_targets(self, targets):
        if not self.entry_active:
            return list(targets)
        result = []
        for pid, path in targets:
            record = self.paths.get(path)
            if not record or record[0] != pid:
                continue
            # The caller has already intersected prose, requested variants and
            # quote quantities.  Do not fill an empty intersection by guessing
            # an exact variant from the product's generic destination.
            candidates = [(pid, path)] if self.allows_path(path) else []
            for candidate in candidates:
                if candidate not in result:
                    result.append(candidate)
        return result

    def _ids(self, text):
        return {p['id'] for p in self.find_products(list(self.products.values()), _visible(text))}

    def _protected(self, pid, text):
        shown = _visible(text)
        if _NEGATIVE.search(shown):
            return True
        # Stating that a product lacks the accepted required feature is a
        # rejection basis, even without a separate "not recommended" verb.
        # It does not protect a simultaneous positive purchase invitation.
        requirements = [value for item in conversation_state.effective_items({'items': self.items}, self.products[pid])
                        if item.get('key') == 'required_features' for value in item.get('value', [])]
        if not _POSITIVE.search(shown) and any(isinstance(value, str) and value
                and re.search(r'(?:没有|不支持|不具备|不提供|无).{0,8}' + re.escape(value), shown)
                and card_constraints.feature_evidence(self.products[pid], value)[0] is False
                for value in requirements):
            return True
        if pid in self.service_products and _SERVICE.search(_visible(text)):
            return True
        if pid not in self.explicit:
            return False
        # Requested general information stays visible; a sibling colour/size
        # is not thereby a requested quote or recommendation.
        return not _has_variant(self.products[pid], _visible(text)) or bool(
            {s['id'] for s in self.resolve_skus(self.ctx, self.products[pid], _visible(text))}
            & self.requested.get(pid, set()))

    def _unsafe(self, text, inherited=(), service_inherited=()):
        ids = self._ids(text)
        named = bool(ids)
        if not ids and inherited and (_CARRY.search(_visible(text)) or any(
                _has_variant(self.products[pid], _visible(text)) for pid in inherited if pid in self.products)):
            ids = set(inherited)
        unsafe = set()
        for pid in ids:
            if (not named and pid in service_inherited
                    and not _RECOMMEND.search(_visible(text))
                    and not re.search(r'可(?:以)?(?:选|买|考虑)|当前|现在|现货', _visible(text))
                    and not _has_variant(self.products[pid], _visible(text))):
                continue
            if self._protected(pid, text):
                continue
            allowed = self.eligible.get(pid, set())
            options = {s['id'] for s in self.resolve_skus(self.ctx, self.products[pid], _visible(text))}
            if not allowed or (_has_variant(self.products[pid], _visible(text)) and not allowed.intersection(options)):
                unsafe.add(pid)
        return ids, unsafe

    def _clean_links(self, text):
        def replace(match):
            path = match.group('path') or match.group('bare')
            if path not in self.paths or self.allows_path(path):
                return match.group()
            label = match.group('label')
            if not label:
                return ''
            pid = self.paths[path][0]
            # Keep requested information as plain text.  A noncompliant
            # unsolicited "商品页" label has no useful content on its own.
            if self._protected(pid, label) or any(self.eligible.get(p) for p in self._ids(label)):
                return label
            return ''
        return _LINK.sub(replace, text)

    def prune_text(self, text):
        if not isinstance(text, str):
            return text
        # A factual future-price commitment is unsupported even in a pure
        # lookup. Current quotes settle only their own SKU, quantity and turn.
        text = recommendation_ownership.ground_future_prices(self, text,
            visible=_visible, link=_LINK, has_variant=_has_variant,
            negative=_NEGATIVE, positive=_POSITIVE, service=_SERVICE)
        if not self.active:
            return self._clean_links(text) if self.entry_active else text
        original = text
        text = self._clean_links(text)
        result = recommendation_ownership.prune(self, text, visible=_visible, link=_LINK,
            has_variant=_has_variant, negative=_NEGATIVE, positive=_POSITIVE,
            service=_SERVICE)
        if result == original:
            return original
        result = result.strip()
        # Deleted destinations may leave only their list separators; keep
        # the surviving link without publishing punctuation-only fragments.
        result = re.sub(r'(?m)^[ \t]*[；;、，,]+(?=\[)', '', result)
        result = re.sub(r'(?<=\))(?:[；;][ \t]*)+(?=$|\n)', '', result)
        if not re.search(r'[A-Za-z0-9\u4e00-\u9fff]', _visible(result)):
            return '刚才提到的规格还不能作为符合当前要求的推荐，需核对符合条件的现货规格后再继续。'
        return result

    def filter_cards(self, cards):
        """Remove only unsolicited, nonconforming shopping cards."""
        if not self.entry_active:
            return list(cards)
        result = []
        for card in cards:
            data = card.get('data') or {}
            pid = data.get('id') if card.get('type') == 'product' else next(
                (p['id'] for p in self.products.values() if any(s.get('id') == data.get('sku_id')
                 for s in p.get('skus', []))), None) if card.get('type') == 'quote' else None
            if pid and pid in self.products and pid not in self.explicit and not self.eligible.get(pid):
                continue
            result.append(card)
        return result


def build_scope(ctx, question, cards=(), facts=(), *, shopping_question=None,
                find_products=None, resolve_skus=None, sku_requests=None, callback_product_ids=()):
    """Build a pure publication scope; agent callers pass their shopping clause.

    Hook signatures match the agent helpers: find_products(products, text),
    resolve_skus(ctx, product, text), sku_requests(ctx, product, text).  Without
    hooks this supports catalogue selectors and a one-item recommendation.
    callback_product_ids is an optional caller-validated customer topic binding;
    it is not a selection or a persistent preference.
    """
    return RecommendationScope(ctx, question, question if shopping_question is None else shopping_question,
                               cards, facts, find_products, resolve_skus, sku_requests, callback_product_ids)
