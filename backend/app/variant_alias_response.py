"""Separate registered colour aliases, variant existence and available stock.

Only the current customer's literal request and fresh catalogue records may
correct an absence claim. No historic shopping preference or assistant text
supplies a missing colour/size. This renderer never authorizes a purchase.
"""
from __future__ import annotations

from datetime import date
import re

from .shopping_state import COLORS, SIZE, color_key, positive_corrections


_COLOUR = re.compile(r'(?:' + COLORS + r'|米白|曜黑|雾绿)')
_BUSINESS = re.compile(r'订单|退款|退货|售后|维修|保修|物流|发票|人工')
_UNCURRENT = re.compile(r'如果|假如|假设|另一家|别家|其他店|其它店|朋友|别人|替他|替她|帮他|帮她')
_NEGATED = re.compile(r'不要|不想|不选|不买|不用|取消|别(?:再)?(?:改|换|买|选)|不是(?:要|想)')
_KEPT = re.compile(r'仍(?:然)?(?:要|选)|保留')


def _names(product):
    return list(dict.fromkeys(str(n) for n in [product.get('id'), product.get('model'),
                          product.get('name'), *product.get('aliases', [])] if n))


def _mentions(text, products):
    candidates = []
    for product in products:
        for name in _names(product):
            pattern = re.escape(name)
            if name.isascii():
                pattern = r'(?<![A-Za-z0-9_-])' + pattern + r'(?![A-Za-z0-9_-])'
            candidates.extend((m.start(), m.end(), product['id'])
                              for m in re.finditer(pattern, text, re.I))
    spans = []
    for start, end, pid in sorted(candidates, key=lambda x: -(x[1]-x[0])):
        if not any(start < b and end > a for a, b, _ in spans):
            spans.append((start, end, pid))
    return sorted(spans)


def _colours(text):
    found = []
    for match in _COLOUR.finditer(text):
        prefix = text[:match.start()]
        # Exact registered colour words may follow a request verb, a product
        # identifier already removed by the caller, or punctuation. An unknown
        # modifier (e.g. 纯白色/珍珠白色) is not the alias 白色.
        if prefix and not re.search(r'[\s，,。；;：:、/()]$|'
                                    r'(?:要|改|换|选|查|看|是|为|有|提供|登记|只有|仅|保留|对应|颜色|色名|色号)$', prefix):
            continue
        if re.search(r'不要|不选|不用|不买|别|取消|假如|如果|假设', prefix):
            continue
        found.append(match.group())
    return list(dict.fromkeys(found))


def _requested_variants(question, products, product_ids):
    mentioned = _mentions(question, products)
    allowed = set(product_ids) if product_ids is not None else None
    segments = []
    if mentioned:
        # Consecutive repeated mentions of the same product belong to one
        # current request; another named product ends its colour inheritance.
        first = []
        for start, end, pid in mentioned:
            if not first or first[-1][2] != pid:
                first.append((start, end, pid))
        for index, (start, end, pid) in enumerate(first):
            if allowed is not None and pid not in allowed:
                continue
            stop = first[index+1][0] if index+1 < len(first) else len(question)
            prior = question[:start]
            if index:
                boundaries = list(re.finditer(r'[，,。；;！？?\n]|另外|此外|顺便|同时', prior))
                prior = prior[boundaries[-1].end():] if boundaries else ''
            segments.append((pid, prior + question[end:stop]))
    elif allowed is not None and len(allowed) == 1:
        # The caller may resolve a current literal product target, but must not
        # supply a historical preference to make an otherwise vague turn match.
        segments.append((next(iter(allowed)), question))
    requests = []
    for pid, text in segments:
        text = re.split(r'另外|此外|顺便|同时|[；;。！？?\n]', text, maxsplit=1)[0]
        if _UNCURRENT.search(text) or _BUSINESS.search(text):
            continue
        clauses = [c for c in re.split(r'[，,]', text) if c.strip()]
        clauses = [c for c in clauses if not _NEGATED.search(c)]
        if not clauses:
            continue
        original = '，'.join(clauses)
        changes = sorted([*positive_corrections(original), *_KEPT.finditer(original)], key=lambda m:m.start())
        current = original[changes[-1].end():] if changes else original
        if not changes and re.search(r'之前|以前|原来|最初|过去', current):
            continue
        colors = _colours(current)
        if len(colors) != 1:
            continue
        sizes = list(dict.fromkeys(s.upper() for s in re.findall(SIZE, current, re.I)))
        if not sizes and re.search(r'尺码不变|尺寸不变|仍(?:然)?同(?:一)?尺码', current):
            sizes = list(dict.fromkeys(s.upper() for s in re.findall(SIZE, original, re.I)))
        if len(sizes) != 1:
            continue
        product = next((p for p in products if p['id'] == pid), None)
        if not product:
            continue
        skus = [s for s in product.get('skus', [])
                if color_key(str(s.get('color') or '')) == color_key(colors[0])
                and str(s.get('size') or '').upper() == sizes[0]]
        if len(skus) == 1:
            requests.append((product, colors[0], sizes[0], skus[0]))
    return requests


def _absence(part, request, *, inherited=False):
    product, requested_color, size, sku = request
    aliases = [word for word in (*COLORS.split('|'), '米白', '曜黑', '雾绿',
                                 requested_color, str(sku.get('color') or ''))
               if word and color_key(word) == color_key(requested_color)]
    # Reuse registered alias equivalence without inferring new spellings.
    aliases = list(dict.fromkeys(aliases))
    named_color = any(word in part for word in aliases)
    generic = inherited and bool(re.search(r'该(?:颜色|规格)|这个?组合|此规格|这一规格', part))
    if not (named_color or generic):
        return False
    if re.search(r'不(?:代表|意味着|等于)|并非|不能(?:说|认为|断言)|不要说|别说', part):
        return False
    if re.search(r'没有(?:预计|确定|记录|核实)?.{0,4}(?:库存|现货|补货|日期)|缺货|库存\s*0', part) \
            and not re.search(r'不存在|匹配不到|组合.{0,8}(?:找不到|无法匹配|不能匹配)', part):
        return False
    if re.search(r'不存在|匹配不到|无法匹配|不能匹配|找不到|没有匹配|无此(?:规格|组合)|不提供', part):
        return True
    return any(re.search(r'(?:没有|无|不含|不售)(?:[「“\s]*)(?:' + re.escape(word) + r')', part)
               for word in aliases)


def _render(request):
    product, requested_color, size, sku = request
    actual_color = str(sku.get('color') or requested_color)
    mapping = (requested_color + '对应目录的' + actual_color
               if requested_color != actual_color else '目录登记颜色为' + actual_color)
    result = f'{product.get("model") or product["id"]}：{mapping}，{size}规格存在（{sku["id"]}）。'
    stock = sku.get('stock')
    if isinstance(stock, int) and not isinstance(stock, bool) and stock >= 0:
        result += f'当前库存{stock}' + ('，现为缺货。' if stock == 0 else '；库存仅为查询时记录，购买前仍需核对所需数量与库存。')
    else:
        result += '当前库存尚未核实，不能确认现货。'
    restock = sku.get('restock_at')
    try:
        known_date = date.fromisoformat(str(restock)) if restock else None
    except ValueError:
        known_date = None
    result += ('目录预计补货日期为' + known_date.isoformat() + '，不能保证供货时间。'
               if known_date else '补货日期未记录，不能保证供货时间。')
    return result


def ground_response(answer, question, products, *, product_ids=None):
    requests = _requested_variants(question, products, product_ids)
    if not requests:
        return answer
    result = answer
    for request in requests:
        product, _, _, _ = request
        parts = re.split(r'(?<=[，,。；;！？?\n])', result)
        scope, changed, replacement = None, False, False
        out = []
        for part in parts:
            ids = {pid for _, _, pid in _mentions(part, products)}
            if ids:
                scope = next(iter(ids)) if len(ids) == 1 else None
            eligible = not ids or ids == {product['id']}
            eligible = eligible and (scope in (None, product['id'])) and not _BUSINESS.search(part)
            false_absence = eligible and _absence(part, request, inherited=changed)
            alternative = changed and eligible and re.search(r'改|换|选', part) \
                and re.search(r'你看|你要|您要|请|还是|[？?]', part) \
                and bool(_COLOUR.search(part))
            if false_absence:
                if not replacement:
                    out.append(_render(request))
                    replacement = True
                changed = True
            elif alternative:
                continue
            else:
                out.append(part)
        if changed:
            result = ''.join(out)
    return result
