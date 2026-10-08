"""Small source-span helpers for requests; no database, model or authorization."""
import re

QUESTION = r'[？?]|吗|呢|如何|怎么|什么|是否|能否|支持|介绍|告诉|哪些|能不能|有没有|想(?:要)?了解|想知道|分别(?:说|说明|介绍|列)|请说明|说说'
DIRECTIVE = r'(?:请|麻烦)(?:再|帮我)?(?:给|提供|附|发|列|查|找)|给我|帮我(?:查|找|选|看|提供|列)|我(?:想|要|希望|需要)(?:额外|免费|再)|额外.{0,8}(?:要|送)|(?:另|再)?加送|免费加送|^查(?:一下)?订单'
NEGATIVE = r'(?:不要|不用|不必|不需要|别|不想)(?:再|给我|帮我)?(?:额外|免费|加送|赠送|赠品|推荐|给|提供|附|发|列|查|找)|取消|撤回'
RECOMMEND = r'推荐|帮我选|帮我找|挑一|(?:给|找|选)(?:我)?(?:一|两|[0-9]+)?(?:款|种)(?:商品)?|替代'
UNASSERTED = r'朋友|别人|替他|替她|他说|她说|你说|您说|假设|比如|例如|举例|以前|之前|曾经'

def clauses(text):
    start = 0
    for match in re.finditer(r'[，,。；;！？?\n]+|另外|另问|顺便|同时|还有', text):
        end = match.end() if match.group()[0] in '！？?' else match.start()
        if text[start:end].strip(): yield start,end,text[start:end].strip()
        start = match.end()
    if text[start:].strip(): yield start,len(text),text[start:].strip()

def _availability_subject(text):
    """A subject is copied from a customer availability query, never invented."""
    suffix=re.search(r'^(?:(?:请|先|帮我|麻烦)?(?:查|看|问)(?:一下)?\s*)?(.+?)(?:有没有|是否有)(?:货)?[？?]?$' ,text)
    if suffix:return suffix[1].strip()
    prefix=re.search(r'(?:有没有|是否有|有|卖)(.+?)(?:吗|么|[？?]|$)',text)
    return prefix[1].strip(' ？?') if prefix else ''



def _unavailable_condition(text):
    text=re.sub(r'^(?:如果|要是|若)', '', text.strip())
    text=re.sub(r'^(?:本店|店里|你们)', '', text)
    text=re.sub(r'(?:的话)?$', '', text).strip()
    # Prefix and suffix availability wording are both conditions, not claims.
    suffix=re.fullmatch(r'(.+?)(?:没有货|没有|没货|缺货|不卖|没卖)',text)
    if suffix:return suffix[1].strip()
    prefix=re.fullmatch(r'(?:没有|没货|缺货|不卖|没卖)(.*)',text)
    return prefix[1].strip() if prefix else None


def conditional_recommendations(text):
    """Preserve explicitly instructed alternatives without activating them.

    A conditional request may be one clause or a condition followed by a
    recommendation. Its availability and budget remain customer statements.
    """
    result=[];previous='';previous_start=0;blocked=False
    for start,end,clause in clauses(text):
        if re.search(UNASSERTED,clause): blocked=True
        if re.search(r'我(?:自己|本人)|现在(?:明确|决定)|实际要求',clause): blocked=False
        body='';subject=None;condition_text='';span_start=start
        split=re.match(r'^(.+?)(?:就|则|再|请)(.+)$',clause)
        if split:
            subject=_unavailable_condition(split[1])
            if subject is not None:
                body=split[2];condition_text=clause[:split.start(2)]
        if subject is None and re.match(r'^(?:的话)?(?:就|则|请|再)',clause):
            subject=_unavailable_condition(previous)
            if subject is not None:
                body=re.sub(r'^(?:的话)?(?:就|则|请|再)', '', clause)
                span_start=previous_start
                condition_text=text[previous_start:start]
        if subject is not None and not subject:
            subject=_availability_subject(previous)
            if subject:condition_text=text[previous_start:start]+condition_text
        if subject in ('这款','这个','该款','该型号','这款商品','这种'):
            subject=_availability_subject(previous)
        if subject and body and not blocked and not re.search(NEGATIVE,body) and re.search(RECOMMEND,body):
            result.append({'start':span_start,'end':end,'text':text[span_start:end],'body':body,
                           'condition':{'kind':'catalogue_unavailable','subject_text':subject,
                                        'source_text':condition_text}})
        previous=clause;previous_start=start
        if end < len(text) and text[end:end+1] in '。；;\n':blocked=False
    return result

def mask_spans(text,spans):
    for span in sorted(spans,key=lambda x:x['start'],reverse=True):
        text=text[:span['start']]+' '*(span['end']-span['start'])+text[span['end']:]
    return text


_QUOTED_REQUEST = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"')
_SERVICE_OBJECT = (
    r'(?:退货|退款|换货|维修|保修|补发|售后)(?:的)?'
    r'(?:资格|条件|评估|审批|申请|草稿|办理|建议|进度|咨询)?|'
    r'(?:退换货|退换)(?:的)?(?:资格|条件|评估|审批|申请|草稿|办理|建议)|'
    r'售后资格|售后申请')
_DENIED_SERVICE = re.compile(
    r'(?:不(?:讨论|评估|判断|考虑|咨询|询问|问|申请|办理|创建|生成|提交)|'
    r'不(?:要|用|需要|想)(?:再)?(?:讨论|评估|判断|考虑|咨询|询问|问|'
    r'申请|办理|创建|生成|提交)?|无需|不必|'
    r'不是(?:再|要|想)(?:讨论|评估|判断|考虑|咨询|询问|问|申请|办理|创建|生成|提交)?)'
    r'\s*(?:' + _SERVICE_OBJECT + r')'
    r'(?:\s*(?:和|或|及|以及|、)\s*(?:' + _SERVICE_OBJECT + r'))*')
_DENIED_SERVICE_TOPICS = re.compile(
    r'(?:不是(?:再|要|想)?(?:问|讨论)|不(?:再)?(?:问|讨论))\s*(?:'
    r'(?:选购|购物|推荐)\s*(?:或|和|及|以及|、)\s*售后|'
    r'售后\s*(?:或|和|及|以及|、)\s*(?:选购|购物|推荐))')


def without_denied_service_requests(text):
    """Mask explicit service-operation denials, preserving source offsets.

    A denial is neither a qualification request nor permission to create work.
    Only the operation and its named service objects are masked: a following
    independent order/price/remedy action remains intact even without commas.
    Quoted wording, conditions, questions and double negatives keep the legacy
    parser path. The original customer message and source text are not edited.
    """
    quoted = [(m.start(), m.end()) for m in _QUOTED_REQUEST.finditer(text)]
    spans = []
    matches = [* _DENIED_SERVICE.finditer(text), * _DENIED_SERVICE_TOPICS.finditer(text)]
    for match in matches:
        if any(start <= match.start() < end for start, end in quoted):
            continue
        clause_start = max((m.end() for m in re.finditer(r'[，,。；;！？?\n]', text[:match.start()])),
                           default=0)
        prefix = text[clause_start:match.start()]
        if re.search(r'如果|假如|假设|要是|(?:^|[，,])若|是否|为什么|为何|'
                     r'是不是|能否|怎么|如何', prefix):
            continue
        if re.search(r'(?:不是|并非|不代表|不能|不应|没有(?:说)?)\s*$', prefix):
            continue
        if re.match(r'\s*(?:吗|么|呢|[？?])', text[match.end():]):
            continue
        spans.append({'start': match.start(), 'end': match.end()})
    return mask_spans(text, spans)

def recommendation_clause(text):
    return bool(re.match(r'^\s*(?:(?:请|麻烦|帮我|给我|然后|再|另外|接着|并且|同时|顺便|直接|现在|还是)\s*)*(?:推荐|帮我选|挑一|(?:给|找)(?:我)?一款|我(?:现在)?(?:就)?(?:想要|要))',text)) and not re.search(NEGATIVE,text)


def gift_object(clause, previous=''):
    """Keep different requested objects independent, using customer words.

    A pronoun/quantity can refer to the immediately preceding receipt clause.
    Unknown objects keep the full clause identity rather than merging requests.
    """
    count=r'(?:一|两|三|四|五|六|七|八|九|十|[0-9]+)(?:根|个|件|条|只|张|套)'
    patterns=[r'(?:一|两|[0-9]+|那|这)(?:根|个|件|条|只|张|套)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{0,14})',
              r'([\u4e00-\u9fffA-Za-z]{1,12}?)(?:'+count+r')?(?:已经|都|已)?(?:收齐|没有缺|不缺|不少)']
    for source in (clause,previous):
        for index,pattern in enumerate(patterns):
            # Receipt predicates after a classifier are not nouns. Overlapping
            # quantity matches still allow a later explicitly requested object.
            for found in re.finditer('(?='+pattern+')' if index==0 else pattern,source):
                value=re.split(r'我|还|可以|能|吗|请|想|要|也|的|已经|不过|但',found[1])[0].strip()
                if re.fullmatch(r'(?:不少|不缺|没有缺|没有少|收齐(?:了)?|齐全|收到了|根|个|件|条|只|张|套|'+count+r')',value):continue
                if value:return {'text':value,'source_text':source}
    return {'text':'','source_text':clause}
