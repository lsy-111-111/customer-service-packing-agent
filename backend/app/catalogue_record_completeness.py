"""Keep a partial record answer from denying other recorded fields.

This pure publication guard receives the caller's confirmed current catalogue.
Only products actually discussed in the answer/current explicit reference can
provide a counterexample. It never adds a guessed field or changes business
facts. Specific unknowns, quotation and hypothetical scopes stay intact.
"""
from __future__ import annotations

import re


_LITERAL = re.compile(r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"')
_ONLY = re.compile(
    r'(?:(?:两款|两者|两种|这两款|这两者|它们|各款|所有商品|上述商品|'
    r'以上商品|本商品|该商品|此商品)\s*)?(?:都|均)?'
    r'(?:只|仅)(?:记录(?:到)?|登记(?:了)?|记载(?:了)?|有)\s*'
    r'(?:以上|上述|这些)(?:的)?(?:信息|内容|参数|字段|记录|资料)'
    r'(?:[，,]\s*(?:其余|其他|其它|剩余)(?:的)?(?:参数|字段|资料|信息|记录)?'
    r'(?:本店|店铺|商品|产品|资料|均|都|尚|目前|当前|中|里|内|的|并|也|\s){0,12}'
    r'(?:未登记|未记录|未记载|没有记录|没有登记))?')
_REMAINDER = re.compile(
    r'(?:其余|其他|其它|剩余)(?:的)?(?:参数|字段|资料|信息|记录)'
    r'(?:本店|店铺|商品|产品|资料|均|都|尚|目前|当前|中|里|内|的|并|也|\s){0,12}'
    r'(?:未登记|未记录|未记载|没有记录|没有登记)')
_TWO = re.compile(r'两(?:款|者|种)|这两')
_ALL = re.compile(r'各款|所有商品|上述商品|以上商品|它们')
_CONDITION = re.compile(r'如果|假如|假设|倘若|若(?:是|只有|仅有|未|不|都)|当.{0,16}时')
_UNKNOWN = re.compile(r'^(?:未知|未记录|未登记|未记载|待核实|不详|暂无(?:记录)?|无法确认)$')
_CORRECTION = '以上仅列出本次提及的记录，未列出的字段不等于未登记；其他字段应按对应商品资料逐项核实'


def _mask_literals(text):
    saved = {}

    def keep(match):
        token = f'＄原文占位{len(saved)}＄'
        saved[token] = match[0]
        return token

    return _LITERAL.sub(keep, text), saved


def _restore(text, saved):
    for token, value in saved.items():
        text = text.replace(token, value)
    return text


def _normal(value):
    return re.sub(r'[\s，,。；;：:、()（）\-–—/]', '', str(value)).casefold()


def _known(value):
    if value is None or value == '' or value == [] or value == {}:
        return False
    if isinstance(value, str) and _UNKNOWN.fullmatch(value.strip()):
        return False
    return True


def _identities(products):
    owners = {}
    for product in products:
        if not isinstance(product, dict) or not product.get('id'):
            continue
        for value in [product['id'], product.get('model'), product.get('name'), *product.get('aliases', [])]:
            if isinstance(value, str) and len(value.strip()) >= 2:
                owners.setdefault(value.strip().casefold(), []).append(product['id'])
    # Ambiguous short aliases do not identify an owner.
    return {value: next(iter(set(ids))) for value, ids in owners.items() if len(set(ids)) == 1}


def _mentions(text, identities):
    found = []
    for word, pid in identities.items():
        boundary = (r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])'
                    if re.fullmatch(r'[A-Za-z0-9 _-]+', word) else re.escape(word))
        found += [(m.start(), m.end(), pid) for m in re.finditer(boundary, text, re.I)]
    selected = []
    for start, end, pid in sorted(found, key=lambda x: (x[0], -(x[1]-x[0]))):
        if any(start < right and end > left for left, right, _ in selected):
            continue
        selected.append((start, end, pid))
    return selected


def _owner_scopes(prefix, question, claim, identities):
    mentions = _mentions(prefix, identities)
    unique = list(dict.fromkeys(pid for _, _, pid in mentions))
    # A named owner immediately before a singular claim wins over old topics.
    clause_start = max([prefix.rfind(ch) for ch in '。！？?；;\n']) + 1
    local = list(dict.fromkeys(pid for _, _, pid in _mentions(prefix[clause_start:], identities)))
    if _TWO.search(claim):
        if len(unique) != 2:
            return {}
        chosen = unique
    elif _ALL.search(claim):
        chosen = unique
    elif len(local) == 1:
        chosen = local
    elif len(unique) == 1:
        chosen = unique
    elif not unique and re.search(r'本商品|该商品|此商品', claim):
        chosen = list(dict.fromkeys(pid for _, _, pid in _mentions(question, identities)))
        if len(chosen) != 1:
            return {}
        return {chosen[0]: prefix}
    else:
        return {}
    if not chosen:
        return {}
    runs = []
    for start, end, pid in mentions:
        if runs and runs[-1][2] == pid:
            continue
        runs.append((start, end, pid))
    scopes = {}
    for n, (start, _, pid) in enumerate(runs):
        if pid not in chosen:
            continue
        end = runs[n+1][0] if n+1 < len(runs) else len(prefix)
        scopes[pid] = scopes.get(pid, '') + prefix[start:end]
    return scopes


def _omitted_known_field(product, scope):
    # "参数/字段" is checked against real spec records, not another product's
    # package, stock, FAQ, policy or order. Known negatives remain records.
    records = product.get('specs') or {}
    if not isinstance(records, dict):
        return False
    text = _normal(scope)
    for field, value in records.items():
        if not _known(value):
            continue
        label = _normal(field)
        literal = _normal(value)
        if label and label in text:
            continue
        if literal and literal in text:
            continue
        return True
    return False


def ground_response(answer, question, products):
    """Correct an exhaustive absence claim only when its actual owners refute it.

    Caller must pass the current verified catalogue, not a saved model summary.
    No source/owner proof means no rewrite. Correct recorded statements are
    retained; a specific unrecorded dimension etc. is never matched here.
    """
    if not isinstance(answer, str) or not answer or not products:
        return answer
    confirmed = {p['id']: p for p in products if isinstance(p, dict) and p.get('id')}
    identities = _identities(list(confirmed.values()))
    text, quoted = _mask_literals(answer)
    requested, _ = _mask_literals(question or '')
    matches = sorted([*list(_ONLY.finditer(text)), *list(_REMAINDER.finditer(text))],
                     key=lambda m: (m.start(), -(m.end()-m.start())))
    edits = []
    for match in matches:
        if any(match.start() < end and match.end() > start for start, end in edits):
            continue
        prefix = text[:match.start()]
        sentence_start = max([prefix.rfind(ch) for ch in '。！？?；;\n']) + 1
        if _CONDITION.search(prefix[sentence_start:]) or _CONDITION.search(match[0]):
            continue
        scopes = _owner_scopes(prefix, requested, match[0], identities)
        if not scopes:
            continue
        if any(_omitted_known_field(confirmed[pid], _restore(scope, quoted))
               for pid, scope in scopes.items()):
            edits.append((match.start(), match.end()))
    for start, end in reversed(edits):
        text = text[:start] + _CORRECTION + text[end:]
    return _restore(text, quoted)
