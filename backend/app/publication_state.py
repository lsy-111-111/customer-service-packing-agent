"""Pure publication projections. Raw conversation and tool records stay intact."""
import re
from decimal import Decimal
from .shopping_state import color_key


def displayed_product_groups(text, products, mentions):
    """Read contiguous product entries independent of Markdown presentation.

    Entries start with a catalogue name/alias (optionally a bullet or number).
    Blank lines are cosmetic; other prose ends the collection. Returned spans
    let headings and comparative claims share one bounded product collection.
    """
    groups, entries, previous, offset = [], [], None, 0
    heading = None

    def finish():
        nonlocal entries, heading
        if entries:
            groups.append({'heading': heading, 'entries': entries,
                           'start': entries[0]['start'], 'end': entries[-1]['end']})
        entries, heading = [], None

    for line in text.splitlines(keepends=True):
        start, offset = offset, offset + len(line)
        if not line.strip():
            continue
        clean = re.sub(r'^\s*(?:(?:[-*+]\s+|\d+[.)、]\s*))?[#*_`\s]*', '', line)
        named = mentions(products, clean)
        aliases = [str(value) for p in named for value in
                   [p.get('name'), p.get('model'), p.get('id'), *p.get('aliases', [])] if value]
        starts_product = any(re.match(re.escape(alias) + (r'(?![A-Za-z0-9])' if alias[-1].isascii() and alias[-1].isalnum() else ''),
                                      clean, re.I) for alias in aliases)
        is_entry = starts_product and not re.search(r'订单|售后|退货|退款|物流|运单|维修', clean)
        if is_entry:
            if not entries and previous and re.search(r'[：:]\s*$', previous['text']):
                heading = previous
            entries.append({'start': start, 'end': offset, 'text': line, 'products': named})
        else:
            finish()
        previous = {'start': start, 'end': offset, 'text': line}
    finish()
    return groups


def preceding_displayed_members(text, position, groups):
    """Only the directly preceding collection supplies comparative members."""
    for group in reversed(groups):
        if group['end'] > position:
            continue
        if text[group['end']:position].strip():
            return []
        return list({p['id']: p for entry in group['entries'] for p in entry['products']}.values())
    return []


class SkuScope:
    """Carry explicit variant dimensions without widening an identified SKU.

    Repeating the same product or adding one dimension preserves the others.
    A different product clears them. Coordinated values remain a set rather
    than becoming an arbitrary last color/size; explicit 'all' clears a filter.
    Quantity is deliberately absent: stock and quote counts are not choices.
    """
    COLORS = r'纯白|冷白|白色|暖白|米白|黑色|曜石黑|曜黑|绿色|鼠尾草绿|米色|燕麦色|燕麦'
    SIZE = r'(?<![A-Za-z0-9])(?:XXL|XL|XS|S|M|L)(?![A-Za-z0-9])'
    CAPACITY = r'\d+(?:\.\d+)?\s*(?:m[lL]|毫升)'

    def __init__(self, products, mentions, select):
        self.products, self.mentions, self.select = products, mentions, select
        self.clear()

    def clear(self):
        self.owner = None
        self.dimensions = {}
        self.pairs = None

    def consume(self, text):
        events = [(a, b, 'product', p) for a, b, p in self.mentions(self.products, text)]
        actual = {kind: {str(s.get(field, '')) for p in self.products for s in p.get('skus', []) if s.get(field)}
                  for kind, field in [('color', 'color'), ('size', 'size')]}
        patterns = {'color': self.COLORS, 'size': self.SIZE, 'capacity': self.CAPACITY}
        for kind in ('color', 'size'):
            literal = [r'(?<![A-Za-z0-9])' + re.escape(v) + r'(?![A-Za-z0-9])'
                       if v.isascii() else re.escape(v) for v in sorted(actual[kind], key=len, reverse=True)]
            patterns[kind] = '|'.join([*literal, patterns[kind]])
        for kind, pattern in patterns.items():
            events.extend((m.start(), m.end(), kind, m[0]) for m in re.finditer(pattern, text, re.I))
        for kind, noun in [('color', '颜色|色号'), ('size', '尺码|尺寸'), ('capacity', '容量')]:
            events.extend((m.start(), m.end(), 'all', kind) for m in re.finditer(
                r'(?:全部|所有|各种|各个|各)(?:的)?(?:' + noun + ')|(?:' + noun + r')(?:不限|不限制)', text))
        for p in self.products:
            for sku in p.get('skus', []):
                pattern = r'(?<![A-Za-z0-9_-])' + re.escape(sku['id']) + r'(?![A-Za-z0-9_-])'
                events.extend((m.start(), m.end(), 'sku', (p, sku)) for m in re.finditer(pattern, text))
        previous = {}
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        for start, end, kind, value in events:
            if kind in ('product', 'sku'):
                product = value if kind == 'product' else value[0]
                if not self.owner or self.owner['id'] != product['id']:
                    # A dimension can precede its noun: “白色 E1”. Bind only
                    # adjacent dimension tokens, never a prior clause's color.
                    prefix, edge = [], start
                    for left, right, dimension, explicit in reversed(events):
                        if right > edge or dimension not in ('color', 'size', 'capacity', 'all'):
                            continue
                        if not re.fullmatch(r'[\s的码]*', text[right:edge]):
                            break
                        prefix.append((dimension, explicit))
                        edge = left
                    self.clear()
                    previous.clear()
                    self.owner = product
                    for dimension, explicit in reversed(prefix):
                        if dimension == 'all':
                            self.dimensions.pop(explicit, None)
                        elif dimension != 'size' or product.get('category') == '服饰':
                            self.dimensions[dimension] = [explicit]
                if kind == 'sku':
                    sku = value[1]
                    self.pairs = None
                    self.dimensions = {k: [v] for k, v in
                                       [('color', sku.get('color')), ('size', sku.get('size'))] if v}
                continue
            if not self.owner:
                continue
            if kind == 'all':
                self.pairs = None
                self.dimensions.pop(value, None)
                previous.pop(value, None)
                continue
            if kind == 'size' and self.owner.get('category') != '服饰':
                continue
            self.pairs = None
            last = previous.get(kind)
            combined = last is not None and re.fullmatch(r'[\s、/／和与及或]+', text[last:start])
            self.dimensions[kind] = [*self.dimensions.get(kind, []), value] if combined else [value]
            previous[kind] = end
        colors = list(re.finditer(patterns['color'], text, re.I))
        if (self.owner and len({p['id'] for _, _, p in self.mentions(self.products, text)}) <= 1
                and len(colors) > 1 and re.search(patterns['size'], text, re.I)):
            paired = {}
            for index, color in enumerate(colors):
                end = colors[index + 1].start() if index + 1 < len(colors) else len(text)
                dimensions = {'color': [color[0]], 'size': re.findall(patterns['size'], text[color.end():end], re.I)}
                paired.update((s['id'], s) for s in self._filter(dimensions))
            self.pairs = list(paired.values())

    def _filter(self, dimensions):
        selected = list(self.owner.get('skus', [])) if self.owner else []
        for kind, values in dimensions.items():
            if not values:
                continue
            if kind == 'color':
                wanted = {color_key('燕麦色' if v == '燕麦' else v) for v in values}
                selected = [s for s in selected if color_key(s.get('color', '')) in wanted]
            elif kind == 'size':
                selected = [s for s in selected if str(s.get('size', '')).upper() in {v.upper() for v in values}]
            elif kind == 'capacity':
                numbers = {Decimal(re.search(r'\d+(?:\.\d+)?', v)[0]) for v in values}
                selected = [s for s in selected if any(Decimal(v) in numbers for v in
                            re.findall(r'(\d+(?:\.\d+)?)\s*(?:m[lL]|毫升)', s.get('label', '')))]
        return selected

    def candidates(self):
        if not self.owner:
            return []
        if self.pairs is not None:
            return self.pairs
        return self._filter(self.dimensions)


LABELS = {'budget_max_cents': '预算上限', 'colors': '颜色偏好', 'excluded_colors': '排除颜色',
          'sizes': '尺码偏好', 'excluded_sizes': '排除尺码', 'usage': '用途',
          'required_features': '必需功能', 'excluded_features': '排除功能或材质',
          'excluded_products': '排除型号'}


def preference_view(state):
    """Explain field operations, never negate the full source utterance."""
    items = []
    for item in state.get('items', []):
        row = {k: item[k] for k in ('key', 'scope', 'product_id', 'active', 'value', 'version') if k in item}
        label = LABELS.get(item.get('key'), item.get('key', '要求'))
        status = item.get('active')
        row['meaning'] = label + ('当前生效' if status is True else
                                  '当前不生效；解除的是这一字段的约束' if status is False
                                  else '缺少明确生效状态，不推断已生效或已解除')
        row['evidence'] = {k: item[k] for k in ('source_message_id', 'source_text') if k in item}
        items.append(row)
    return {**state, 'items': items,
            'interpretation': 'active=false仅指该key约束已解除，不是否定或撤销source_text整句话。'
            '解除排除功能/材质表示当前没有该排除要求，不能说顾客撤销了接受；'
            '解除排除型号不表示撤销新购买选择。其他字段未改则保持原状态。'}


def preference_summary(ctx, scope):
    state, products = ctx.get('preferences', {}), ctx.get('products', [])
    by_id = {p['id']: p for p in products}
    clauses = []
    for item in state.get('items', []):
        if item.get('scope') not in ('会话', scope):
            continue
        key = item.get('key')
        if key not in LABELS:
            continue
        product = by_id.get(item.get('product_id'))
        prefix = (product.get('model') or product['id']) + '的' if product else ''
        if item.get('active') is False:
            clauses.append(prefix + LABELS[key] + '当前不生效')
            continue
        if item.get('active') is not True:
            continue
        value = item.get('value')
        if key == 'budget_max_cents' and type(value) is int:
            display = f'{value / 100:g}元'
        elif isinstance(value, list):
            display = '、'.join(str(v) for v in value)
        else:
            display = str(value) if value is not None else ''
        if display:
            clauses.append(prefix + LABELS[key] + '为' + display)
    for choice in state.get('shopping', {}).get('selections', []):
        product = by_id.get(choice.get('product_id'))
        if not product or product.get('category') != scope or choice.get('selection_kind') != 'explicit_choice':
            continue
        details = [product.get('model') or product['id'], *choice.get('colors', []), *choice.get('sizes', [])]
        if type(choice.get('quantity')) is int:
            details.append(str(choice['quantity']) + '件')
        clauses.append('当前明确选择为' + ' / '.join(details))
    return '本次选购要求：' + '；'.join(dict.fromkeys(clauses)) + '。' if clauses else ''
