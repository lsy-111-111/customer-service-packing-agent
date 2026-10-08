"""Ground personal size statements in this conversation's actual user words.

Catalog sizes, selections, profiles and prior generated replies are not body
or usual-size evidence. This module is pure and does not infer physical fit.
"""
import re
from decimal import Decimal


_SIZE = (r'(?<![A-Za-z0-9])(?:XXXL|XXL|XL|XS|S|M|L|\d{2,3})'
         r'(?![A-Za-z0-9]|\s*(?:元|块|人民币|件|条|副|个|双|台|套))')
_SIZES = re.compile(_SIZE, re.I)
_HABIT = r'(?:平时|日常|通常|一般|一直|经常|常常|惯常|以前|原来|常穿)'
_HABIT_PREDICATE = (
    r'(?:' + _HABIT + r'\s*(?:都|通常|一般|的)?'
    r'(?:穿(?:着|的)?|的尺码|尺码|的码|码)'
    r'[^，,。；;！？?\n]{0,26}?' + _SIZE + r'\s*(?:码|号)?|'
    r'常穿[^，,。；;！？?\n]{0,26}?' + _SIZE + r'\s*(?:码|号)?|'
    # A standalone alphabetic size is accepted as the ordinary shorthand
    # "平时M"; it cannot consume a budget, quantity, model or intervening verb.
    r'(?:平时|日常|通常|一般)\s*(?:是|为)?\s*'
    r'(?<![A-Za-z0-9])(?:XXXL|XXL|XL|XS|S|M|L)(?![A-Za-z0-9])\s*(?:码|号)?'
    r'(?=$|[，,。；;！？?\n\s])' + r')')
_HABIT_CLAIM = re.compile(
    r'(?:你|您)(?:说过|说|提到过)?(?:自己)?(?:的)?' + _HABIT_PREDICATE, re.I)
_MEASURE = re.compile(
    r'(?P<field>身高|体重|胸围|腰围|肩宽)\s*(?:是|为|[:：]|大约|约|有|只有)?\s*'
    r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>厘米|公分|毫米|千克|公斤|cm|mm|kg|米|斤|m)?', re.I)
_MEASURE_CLAIM = re.compile(r'(?:你|您)(?:的)?\s*' + _MEASURE.pattern, re.I)
_BUILD = r'(?:(?:比较|较|很|有点|有些|偏)?(?:瘦|胖|壮)|高挑|娇小|丰满|匀称|高个子|小个子|宽肩)'
_BUILD_CLAIM = re.compile(
    r'(?:你|您)(?:的)?(?:身材|体型)?(?:是|属于|看起来)?\s*(?P<build>' + _BUILD + r')')
_QUOTES = re.compile(r'“[^”]*”|‘[^’]*’|"[^"]*"|\'[^\']*\'|`[^`]*`')
_NONASSERTIVE = re.compile(
    r'如果|假如|假设|假定|假想|要是|比如|例如|举例|例子|示例|可能|不确定|不知道|尚未|未确认|'
    r'不能(?:认定|确认|确定|判断)|无法(?:认定|确认|确定|判断)|并非|并不是|不是|'
    r'没有|没穿|不穿|不再穿|从不|从没|否认|撤回|说错|不代表|不等于')
_OTHER_PERSON = re.compile(r'朋友|同事|他|她|孩子|爸爸|妈妈|父亲|母亲|老公|老婆|男友|女友|顾客')
_CATALOG_CONTEXT = re.compile(r'尺码表|商品|产品|目录|规格|本款|这件|这款|衣长|T\d|[SML]码', re.I)
_HABIT_DETAILS = re.compile(r'常规|直筒|合身|修身|宽松|别家|其他品牌|其他牌子|外套|衬衫|T恤|牛仔裤|裤子|正码|偏大|偏小')
_FIT = re.compile(
    r'(?:你|您)(?:更|很|完全|肯定|一定|通常|一般|就|会)?适合\s*' + _SIZE +
    r'\s*(?:码|号)?[^，,。；;！？?\n]*|'
    + _SIZE + r'\s*(?:码|号)?(?:更|很|完全|肯定|一定|通常|一般|就|会)?适合(?:你|您)'
    r'[^，,。；;！？?\n]*|'
    r'(?:你|您)(?:这(?:个|种|样)|的)?(?:身材|体型)[^，,。；;！？?\n]{0,24}'
    r'(?:适合|驾驭|合身|合适)[^，,。；;！？?\n]*|'
    r'(?:这件|这款|你穿|您穿|你选|您选|你按|您按|按|选|穿)\s*'
    r'[^，,。；;！？?\n]{0,12}?' + _SIZE + r'\s*(?:码|号)?'
    r'[^，,。；;！？?\n]{0,12}?(?:够松|合身|合适|适合你|适合您|没问题|更宽松|更修身|偏大|偏小)'
    r'[^，,。；;！？?\n]*|'
    r'(?:按|选)(?:你|您)?(?:的)?(?:平时|日常|常穿)(?:的)?尺码(?:选)?'
    r'[^，,。；;！？?\n]{0,12}(?:有余量|够松|合身|合适|没问题)[^，,。；;！？?\n]*', re.I)
_SHIFT = re.compile(r'(?:可以|建议|应该|可|应|就)(?:考虑|选择|选|买|穿)?\s*(?:小|大)(?:一|两|1|2)(?:码|号)')
_FIT_LIMIT = '具体合身程度需对照本款尺码表和实测尺寸核对'


def _clauses(text):
    """Keep delimiters and source offsets, including question punctuation."""
    pieces = re.split(r'([，,。；;！？?\n]+)', text)
    offset = 0
    for index in range(0, len(pieces), 2):
        clause = pieces[index]
        ending = pieces[index + 1] if index + 1 < len(pieces) else ''
        yield clause, ending, offset
        offset += len(clause) + len(ending)


def _number(field, value, unit):
    value = Decimal(value)
    unit = (unit or ('kg' if field == '体重' else 'cm')).lower()
    if field == '体重' and unit not in ('kg', '公斤', '千克', '斤'):
        return None
    if field != '体重' and unit not in ('cm', '厘米', '公分', 'm', '米', 'mm', '毫米'):
        return None
    if unit in ('米', 'm'):
        value *= 100
    elif unit in ('毫米', 'mm'):
        value /= 10
    elif unit == '斤':
        value /= 2
    return format(value.normalize(), 'f'), ('kg' if field == '体重' else 'cm')


def _user_evidence(ctx):
    usual, usual_words, measures, build = None, '', {}, None
    conversation_id = ctx.get('conversation_id')
    for message in ctx.get('messages', []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        if str(message.get('status', '')).lower() in ('failed', 'cancelled', 'canceled'):
            continue
        if (conversation_id and message.get('conversation_id') and
                message['conversation_id'] != conversation_id):
            continue
        raw = message.get('content')
        if not isinstance(raw, str):
            continue
        for quote in _QUOTES.finditer(raw):
            prefix = re.split(r'[，,。；;！？?\n]', raw[:quote.start()])[-1]
            suffix = re.split(r'[。；;！？?\n]', raw[quote.end():], 1)[0]
            target = quote.group()[1:-1]
            withdrawal = re.search(r'撤回|撤销|不要沿用|忽略|忘掉|说错', prefix) or re.search(
                r'(?:这句|这句话|它|那|之前|刚才).{0,8}(?:不是事实|不属实|是假的|说错)', suffix)
            personal_habit = re.search(r'(?:我(?:自己|本人)?|本人|自己)(?:的)?' + _HABIT_PREDICATE, target, re.I)
            if (not withdrawal or _OTHER_PERSON.search(prefix + target)
                    or (_CATALOG_CONTEXT.search(target) and not personal_habit)):
                continue
            # A quoted statement cannot create evidence, but an actual user
            # directive to withdraw their quoted statement invalidates it.
            if re.search(_HABIT + r'.{0,8}(?:穿|尺码|码)|常穿', target):
                usual, usual_words = None, ''
            for field in re.findall(r'身高|体重|胸围|腰围|肩宽', target):
                measures.pop(field, None)
            if re.search(r'身材|体型', target):
                build = None
        # Quoted examples and an instruction to fabricate a statement do not
        # establish that the quoted body/size belongs to the customer.
        def unquote(match):
            inner = match.group()[1:-1]
            if (re.fullmatch(r'\s*' + _SIZE + r'\s*(?:码|号)?\s*', inner, re.I)
                    or re.fullmatch(r'\s*\d+(?:\.\d+)?\s*(?:厘米|公分|毫米|千克|公斤|cm|mm|kg|米|斤|m)\s*', inner, re.I)):
                return inner  # "我平时穿‘M’码" is an actual user statement.
            return ''
        raw = _QUOTES.sub(unquote, raw)
        inherited_other, inherited_qualified, inherited_catalog = False, False, False
        for clause, ending, _ in _clauses(raw):
            explicit_self = bool(re.search(r'我(?:自己|本人)?|本人|自己', clause))
            current_other = bool(_OTHER_PERSON.search(clause))
            inherited_other = current_other or (inherited_other and not explicit_self)
            inherited_catalog = bool(_CATALOG_CONTEXT.search(clause)) or (inherited_catalog and not explicit_self)
            qualified = bool(inherited_qualified or _NONASSERTIVE.search(clause) or
                             re.search(r'[？?]|吗|么|是否|还是', ending + clause) or
                             re.search(r'请.{0,3}(?:回答|说|写)|编造|假装|说成|写成', clause))
            inherited_qualified = qualified
            other, catalog = inherited_other, inherited_catalog
            if re.search(r'[。；;！？?\n]', ending):
                inherited_other, inherited_qualified, inherited_catalog = False, False, False
            if not clause.strip() or other:
                continue
            habitual = re.search(
                r'(?:我(?:自己|本人)?|本人|自己|^)\s*(?:的)?[^，,。；;！？?\n]{0,8}?' +
                _HABIT_PREDICATE, clause, re.I)
            codes = _SIZES.findall(habitual.group()) if habitual else []
            correction = bool(re.search(r'更正|纠正|撤回|说错|不要沿用|忽略|忘掉|不再', clause))
            if correction and not catalog:
                for field in re.findall(r'身高|体重|胸围|腰围|肩宽', clause):
                    measures.pop(field, None)
                if re.search(r'身材|体型', clause):
                    build = None
            if habitual:
                if qualified:
                    if correction or re.search(r'不是|并非|不穿|没穿|不再穿|从不|不知道|不确定', clause):
                        usual, usual_words = None, ''
                elif len(codes) == 1:
                    usual, usual_words = codes[0].upper(), clause
            elif (correction or re.search(r'不知道|不确定|不是|并非|不穿', clause)) and re.search(
                    _HABIT + r'.{0,8}(?:穿|尺码|码)|常穿|以前.{0,4}尺码', clause):
                usual, usual_words = None, ''
            personal_measure = bool(re.search(
                r'(?:我(?:自己|本人)?|本人|自己)(?:的)?[^，,。；;！？?\n]{0,8}(?:身高|体重|胸围|腰围|肩宽)|'
                r'^\s*(?:身高|体重|胸围|腰围|肩宽)', clause))
            if personal_measure and not catalog:
                if qualified and (correction or re.search(r'不是|并非|不知道|不确定', clause)):
                    for field in re.findall(r'身高|体重|胸围|腰围|肩宽', clause):
                        measures.pop(field, None)
                for match in _MEASURE.finditer(clause):
                    field = match['field']
                    if qualified:
                        if correction or re.search(r'不是|并非|不知道|不确定', clause):
                            measures.pop(field, None)
                    else:
                        number = _number(field, match['value'], match['unit'])
                        if number is not None:
                            measures[field] = number
            personal_build = re.search(
                r'(?:我(?:的)?|本人(?:的)?|自己(?:的)?)(?:身材|体型)?'
                r'(?:并不是|不是|并非|不再|不|是|属于|看起来)?\s*(?P<build>' + _BUILD + r')', clause)
            if personal_build:
                if qualified:
                    if correction or re.search(r'不是|并非|不知道|不确定', clause):
                        build = None
                else:
                    build = personal_build['build']
    return usual, usual_words, measures, build


def _qualified(clause, ending, match, offset, quotes):
    if any(start <= offset + match.start() < end for start, end in quotes):
        return True
    prefix, suffix = clause[:match.start()], clause[match.end():]
    return bool(_NONASSERTIVE.search(prefix) or
                re.search(r'(?:的话|的情况下|为例|仅为示例)', suffix) or
                re.search(r'[？?]|吗|么|是否|能否', ending + suffix))


def ground_response(text: str, ctx: dict) -> str:
    """Replace unsupported personal assertions without changing size tables.

    A sourced usual-size statement remains a statement, never evidence that a
    garment must fit or that a size shift is safe. The caller owns catalog and
    business validation; this function neither reads nor mutates those inputs.
    """
    if not text:
        return text
    usual, usual_words, measures, build = _user_evidence(ctx or {})
    quotes = [(m.start(), m.end()) for m in _QUOTES.finditer(text)]
    parts = []
    personal_body_scope, body_qualified_scope = False, False
    for clause, ending, offset in _clauses(text):
        replacements = []
        direct_measures = list(_MEASURE_CLAIM.finditer(clause))
        direct_builds = list(_BUILD_CLAIM.finditer(clause))
        catalog_clause = bool(_CATALOG_CONTEXT.search(clause))
        if catalog_clause:
            personal_body_scope, body_qualified_scope = False, False
        if direct_measures or direct_builds or re.search(
                r'(?:你|您)(?:的)?(?:身高|体重|胸围|腰围|肩宽|身材|体型)', clause):
            personal_body_scope = True
        if personal_body_scope and re.search(
                r'如果|假如|假设|假定|假想|要是|比如|例如|举例|示例|'
                r'不能(?:认定|确认|确定|判断)|无法(?:认定|确认|确定|判断)', clause):
            body_qualified_scope = True
        habit_matches = list(_HABIT_CLAIM.finditer(clause))
        supported_habits = set()
        for match in habit_matches:
            if _qualified(clause, ending, match, offset, quotes):
                continue
            codes = _SIZES.findall(match.group())
            details = _HABIT_DETAILS.findall(match.group())
            if len(codes) == 1 and codes[0].upper() == usual and all(d in usual_words for d in details):
                supported_habits.add(match.span())
                continue
            grounded = f'你明确说过平时穿 {usual} 码' if usual else '你的平时尺码尚未确认'
            replacements.append((match.start(), match.end(), grounded))
        measure_claims = [*direct_measures]
        if personal_body_scope and not catalog_clause:
            measure_claims.extend(match for match in _MEASURE.finditer(clause)
                                  if re.search(r'(?:^|[、/])\s*$', clause[:match.start()]))
        for match in measure_claims:
            if body_qualified_scope or _qualified(clause, ending, match, offset, quotes):
                continue
            field = match['field']
            actual = measures.get(field)
            claimed = _number(field, match['value'], match['unit'])
            if actual is not None and actual == claimed:
                continue
            grounded = (f'你明确提供的{field}是 {actual[0]} {actual[1]}'
                        if actual else f'你的{field}尚未确认')
            replacements.append((match.start(), match.end(), grounded))
        for match in direct_builds:
            if body_qualified_scope or _qualified(clause, ending, match, offset, quotes) or match['build'] == build:
                continue
            grounded = f'你明确描述的体型是{build}' if build else '你的体型尚未确认'
            replacements.append((match.start(), match.end(), grounded))
        for pattern in (_FIT, _SHIFT):
            for match in pattern.finditer(clause):
                if _qualified(clause, ending, match, offset, quotes):
                    continue
                if re.search(r'未必|不一定|不能|无法|不保证|尚不能', match.group()):
                    continue
                if re.search(r'(?:核对|对照|比较|查看|看)[^，,。；;！？?\n]{0,12}$', clause[:match.start()]):
                    continue
                if re.search(r'预算|价格|金额|数量|件数|件套', match.group()):
                    continue
                # Desired fit is a preference, not a measured fit conclusion.
                overlap = next((habit for habit in habit_matches
                                if habit.start() <= match.start() < habit.end() <= match.end()), None)
                if overlap:
                    prefix = (overlap.group() if overlap.span() in supported_habits else
                              f'你明确说过平时穿 {usual} 码' if usual else '你的平时尺码尚未确认')
                    replacements.append((overlap.start(), match.end(), prefix + '，' + _FIT_LIMIT))
                elif pattern is _SHIFT and re.fullmatch(r'\s*(?:想|喜欢|希望|为了|偏好)[^，,。；;！？?\n]*', clause[:match.start()]):
                    replacements.append((0, len(clause), _FIT_LIMIT))
                else:
                    replacements.append((match.start(), match.end(), _FIT_LIMIT))
        # Broader fit limitations take precedence over nested body claims.
        chosen = []
        for item in sorted(replacements, key=lambda r: (-(r[1] - r[0]), r[0])):
            if not any(item[0] < old[1] and old[0] < item[1] for old in chosen):
                chosen.append(item)
        for start, end, replacement in sorted(chosen, reverse=True):
            clause = clause[:start] + replacement + clause[end:]
        parts.append(clause + ending)
        if re.search(r'[。；;！？?\n]', ending):
            personal_body_scope, body_qualified_scope = False, False
    return ''.join(parts)
