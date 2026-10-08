"""Business boundary: all mutations are authorized, versioned and transactional.
The model only receives the explicitly enumerated tool gateway below.
"""
import json
import re
import secrets
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from . import db
from . import policy as policy_engine, facts as case_facts
from .config import settings


def js(value):
    return json.dumps(value, ensure_ascii=False)


def fail(status, detail):
    raise HTTPException(status, detail)


def one(c, sql, params=(), message='记录不存在'):
    if any(not isinstance(value,(str,int,float,bytes,type(None))) for value in params):
        fail(422,'标识或查询参数格式不正确')
    row = c.execute(sql, params).fetchone()
    if not row:
        fail(404, message)
    return row


def require_staff(user):
    if user['role'] != 'staff':
        fail(403, '这个操作需要人工客服身份')


def require_customer(user):
    if user['role'] != 'customer':
        fail(403, '请使用顾客身份进行此操作')


def owner(user, user_id):
    if user['role'] != 'staff' and user['id'] != user_id:
        fail(404, '记录不存在或无权访问')


def positive(value, name='数量', minimum=1, maximum=99):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        fail(422, f'{name}应为 {minimum}–{maximum} 的整数')
    return value


def request_key(data, field='request_id'):
    val = data.get(field)
    if not isinstance(val, str) or not val.strip() or len(val) > 200:
        fail(422, f'缺少有效的 {field}')
    return val


def memo(c, user, op, req):
    row=c.execute('SELECT data FROM idempotency WHERE user_id=? AND operation=? AND request_id=?',(user['id'],op,req)).fetchone()
    return json.loads(row[0]) if row else None


def remember(c,user,op,req,value):
    c.execute('INSERT INTO idempotency VALUES (?,?,?,?)',(user['id'],op,req,js(value)))
    return value


def audit(c,user,action,target,before,after):
    c.execute('INSERT INTO audit VALUES (?,?,?,?,?,?,?)',(db.uid('audit'),user['id'],action,target,js(before) if before is not None else None,js(after),db.now()))


def personas():
    with db.connect() as c:
        return [json.loads(r[0]) for r in c.execute('SELECT data FROM users ORDER BY rowid')]


def create_customer(data):
    name = data.get('name')
    if not isinstance(name, str):
        fail(422, '请填写顾客名字')
    name = name.strip()
    if not 1 <= len(name) <= 20:
        fail(422, '顾客名字需要 1–20 个字符')
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in name):
        fail(422, '顾客名字不能包含换行或控制字符')
    # BEGIN IMMEDIATE serializes the check and insert, including across processes.
    with db.transaction() as c:
        for row in c.execute('SELECT data FROM users'):
            if json.loads(row[0])['name'].strip().casefold() == name.casefold():
                fail(409, '这个名字已经存在，请换一个名字')
        customer = {'id': db.uid('customer'), 'name': name, 'role': 'customer',
                    'description': '你添加的顾客', 'avatar': name[:2]}
        c.execute('INSERT INTO users VALUES (?,?)', (customer['id'], js(customer)))
        return customer


def login(persona_id):
    with db.transaction() as c:
        user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(persona_id,),'请选择一个已有身份')[0])
        token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions VALUES (?,?,?)',(token,user['id'],db.now()))
        return {'token':token,'user':user}


def authenticate(token):
    with db.connect() as c:
        row=c.execute('SELECT users.data FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=?',(token,)).fetchone()
        if not row:
            fail(401,'请先选择身份进入店铺')
        return json.loads(row[0])


def _sku(c, sid):
    return dict(one(c,'SELECT * FROM skus WHERE id=?',(sid,),'没有找到这个商品规格'))


def _product(c,pid):
    from . import catalog
    p=json.loads(one(c,'SELECT data FROM products WHERE id=?',(pid,),'没有找到这款商品')[0])
    p['skus']=[dict(r) for r in c.execute('SELECT * FROM skus WHERE product_id=? ORDER BY rowid',(pid,))]
    p['parts']=[dict(r) for r in c.execute('SELECT * FROM part_skus WHERE product_id=? ORDER BY rowid',(pid,))]
    if p['skus']:
        p['price_cents']=min(s['price_cents'] for s in p['skus'])
    return catalog.decorate_product(p)


def _products(c,search='',category=''):
    result=[]
    for r in c.execute('SELECT id FROM products ORDER BY rowid'):
        p=_product(c,r[0])
        if category and category not in ('全部','all') and p['category']!=category:
            continue
        if search:
            hay=js({k:p[k] for k in ['name','model','category','description','aliases','specs']}).lower()
            tokens=search.lower().strip().split()
            if not all(token in hay for token in tokens):
                continue
        result.append(p)
    return result


def products(search='',category=''):
    with db.connect() as c:
        return _products(c,search,category)


def product(pid):
    with db.connect() as c:
        return _product(c,pid)


def inventory(sid):
    with db.connect() as c:
        part=c.execute('SELECT * FROM part_skus WHERE id=?',(sid,)).fetchone()
        return dict(part) if part else _sku(c,sid)


def _parts(c,order):
    result=[]
    for item in order.get('package_snapshot',[]):
        if item.get('kind')!='part':continue
        row=c.execute('SELECT * FROM part_skus WHERE id=?',(item.get('part_sku_id'),)).fetchone()
        if row:result.append({**dict(row),'included_quantity':item.get('quantity',1),'order_quantity':order['quantity']})
    return result


def get_parts(user,order_id):
    with db.connect() as c:
        o=_order(c,user,order_id)
        return {'order_id':order_id,'package_snapshot':o.get('package_snapshot',[]),'parts':_parts(c,o)}


def _policy(c,version=None):
    row=one(c,'SELECT data FROM policies WHERE version=?',(version,)) if version else one(c,'SELECT data FROM policies ORDER BY version DESC LIMIT 1')
    return json.loads(row[0])


def policy():
    with db.connect() as c:
        return _policy(c)


def _promotions(c):
    return json.loads(one(c,'SELECT data FROM promotions WHERE id=1')[0])


def promotions():
    with db.connect() as c:
        return _promotions(c)


def _quote(c,user,sid,quantity,persist=True):
    positive(quantity)
    s=_sku(c,sid)
    p=_promotions(c)
    subtotal=s['price_cents']*quantity
    discount=0
    label='当前售价'
    stamp=db.now()
    if p['enabled'] and p['starts_at']<=stamp<=p['ends_at']:
        reduction=p['reduction_cents'] if subtotal>=p['threshold_cents'] else 0
        special=max(0,subtotal-p['special_price_cents']*quantity) if s['product_id']==p['special_product_id'] else 0
        if special>reduction:
            discount=special;label='商品活动价（与满减互斥，已择优）'
        elif reduction:
            discount=min(reduction,subtotal)
            label=f"满 ¥{p['threshold_cents']/100:g} 减 ¥{p['reduction_cents']/100:g}"
            if s['product_id']==p['special_product_id']:
                label+='（与商品活动价互斥，已择优）'
    q={'sku_id':sid,'quantity':quantity,'unit_price_cents':s['price_cents'],'subtotal_cents':subtotal,'discount_cents':discount,'total_cents':subtotal-discount,'promotion_label':label,'stock':s['stock'],'sku_version':s['version'],'promotion_version':p['version'],'quote_id':db.uid('quote')}
    if persist:
        c.execute('INSERT INTO quotes VALUES (?,?,?,?)',(q['quote_id'],user['id'],stamp,js(q)))
    return q


def quote(user,data):
    with db.transaction() as c:
        return _quote(c,user,data.get('sku_id'),data.get('quantity'))


def _order(c,user,oid):
    row=one(c,'SELECT * FROM orders WHERE id=?',(oid,),'没有找到这笔订单')
    owner(user,row['user_id'])
    o=json.loads(row['data'])
    reserved=0
    for r in c.execute("SELECT data FROM cases WHERE order_id=? AND status IN ('submitted','needs_info','approved','completed')",(oid,)):
        case=json.loads(r[0])
        if case.get('reship_kind')!='part':reserved+=case['quantity']
    o['remaining_quantity']=max(0,o['quantity']-reserved)
    return o


def order(user,oid):
    with db.connect() as c:
        return _order(c,user,oid)


def _orders(c,user):
    rows=c.execute('SELECT id FROM orders ORDER BY rowid DESC') if user['role']=='staff' else c.execute('SELECT id FROM orders WHERE user_id=? ORDER BY rowid DESC',(user['id'],))
    return [_order(c,user,r[0]) for r in rows]


def orders(user):
    with db.connect() as c:
        return _orders(c,user)


def _move(c,sid,delta,reason,reference):
    table='part_skus' if c.execute('SELECT 1 FROM part_skus WHERE id=?',(sid,)).fetchone() else 'skus'
    if delta<0:
        changed=c.execute(f'UPDATE {table} SET stock=stock+?,version=version+1 WHERE id=? AND stock>=?',(delta,sid,-delta)).rowcount
        if not changed:
            fail(409,'该规格库存不足，请重新选择规格或等待补货')
    else:
        c.execute(f'UPDATE {table} SET stock=stock+?,version=version+1 WHERE id=?',(delta,sid))
    c.execute('INSERT INTO inventory_movements VALUES (?,?,?,?,?,?)',(db.uid('stock'),sid,delta,reason,reference,db.now()))


def create_order(user,data):
    require_customer(user)
    req=request_key(data)
    with db.transaction() as c:
        cached=memo(c,user,'order',req)
        if cached:return cached
        row=one(c,'SELECT * FROM quotes WHERE id=?',(data.get('quote_id'),),'报价不存在，请重新查看价格')
        if row['user_id']!=user['id']:fail(404,'报价不存在')
        q=json.loads(row['data'])
        current=_quote(c,user,q['sku_id'],q['quantity'],False)
        if datetime.fromisoformat(row['created_at'])<datetime.now(timezone.utc)-timedelta(minutes=30):
            fail(409,'报价已过期，请重新查看价格')
        if any(current[k]!=q[k] for k in ['sku_version','promotion_version','total_cents']):
            fail(409,'价格、活动或库存已更新，请重新确认报价')
        s=_sku(c,q['sku_id']);p=_product(c,s['product_id']);oid=db.uid('order');stamp=db.now()
        _move(c,s['id'],-q['quantity'],'模拟下单',oid)
        result={'id':oid,'user_id':user['id'],'sku_id':s['id'],'product_id':p['id'],'product_name':p['name'],'sku_label':s['label'],'image':p['image'],'quantity':q['quantity'],'unit_price_cents':q['unit_price_cents'],'discount_cents':q['discount_cents'],'total_cents':q['total_cents'],'status':'delivered','created_at':stamp,'delivered_at':stamp,'policy_version':_policy(c)['version'],'remaining_quantity':q['quantity']}
        result['package_snapshot']=p.get('package_items',[])
        result['package_snapshot_source']='purchase_snapshot'
        c.execute('INSERT INTO orders VALUES (?,?,?)',(oid,user['id'],js(result)))
        audit(c,user,'创建模拟已签收订单',oid,None,result)
        return remember(c,user,'order',req,result)


def _conversation(c,user,cid):
    row=one(c,'SELECT * FROM conversations WHERE id=?',(cid,),'会话不存在')
    owner(user,row['user_id'])
    val=dict(row)
    u=json.loads(one(c,'SELECT data FROM users WHERE id=?',(row['user_id'],))[0])
    last=c.execute('SELECT content FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1',(cid,)).fetchone()
    val.update(user_name=u['name'],last_message=last[0] if last else '',unread_count=0)
    return val


def _message(row):
    m=dict(row)
    m['attachments']=json.loads(m['attachments']);m['cards']=json.loads(m['cards'])
    m.pop('client_message_id',None)
    return m


def _add_message(c,cid,role,content,attachments=None,cards=None,turn_id=None,client_message_id=None,status='completed'):
    mid=db.uid('msg');stamp=db.now()
    c.execute('INSERT INTO messages (id,conversation_id,role,content,attachments,cards,created_at,status,turn_id,client_message_id) VALUES (?,?,?,?,?,?,?,?,?,?)',(mid,cid,role,content,js(attachments or []),js(cards or []),stamp,status,turn_id,client_message_id))
    c.execute('UPDATE conversations SET updated_at=? WHERE id=?',(stamp,cid))
    return _message(one(c,'SELECT * FROM messages WHERE id=?',(mid,)))


def create_conversation(user,fresh=False):
    require_customer(user)
    with db.transaction() as c:
        old=c.execute('SELECT id FROM conversations WHERE user_id=? ORDER BY rowid LIMIT 1',(user['id'],)).fetchone()
        if old and not fresh:return _conversation(c,user,old[0])
        cid=db.uid('conv')
        c.execute('INSERT INTO conversations (id,user_id,updated_at) VALUES (?,?,?)',(cid,user['id'],db.now()))
        if c.execute('SELECT 1 FROM orders WHERE user_id=? LIMIT 1',(user['id'],)).fetchone():
            _add_message(c,cid,'assistant',f"你好，{user['name']}。欢迎来日常好物。想找一件适合的商品，或是处理订单的小问题，都可以和我聊聊。")
        return _conversation(c,user,cid)


def conversations(user):
    with db.connect() as c:
        rows=c.execute('SELECT id FROM conversations ORDER BY updated_at DESC') if user['role']=='staff' else c.execute('SELECT id FROM conversations WHERE user_id=? ORDER BY updated_at DESC',(user['id'],))
        return [_conversation(c,user,r[0]) for r in rows]


def _turn(c,user,tid):
    row=one(c,'SELECT * FROM turns WHERE id=?',(tid,),'没有找到这次回复')
    owner(user,row['user_id'])
    t=dict(row)
    for k in ['user_id','generation','usage','elapsed_ms','updated_at']:t.pop(k,None)
    return t


def turn(user,tid):
    with db.connect() as c:return _turn(c,user,tid)


def conversation_detail(user,cid):
    from . import conversation_state,support
    with db.transaction() as c:
        conv=_conversation(c,user,cid)
        rows=c.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid',(cid,)).fetchall()
        cases=[_case(c,user,r[0]) for r in c.execute('SELECT id FROM cases WHERE conversation_id=? ORDER BY rowid DESC',(cid,))]
        latest=c.execute("SELECT id FROM turns WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1",(cid,)).fetchone()
        preferences=conversation_state.safe_sync(c,conv,_products(c))
        result={'conversation':conv,'messages':[_message(r) for r in rows],'cases':cases,'active_turn':_turn(c,user,latest[0]) if latest else None,'preferences':preferences,'intents':conversation_state.intents(c,cid)}
    result.update(support.conversation_support(user,cid))
    return result


def set_context(user,cid,data):
    require_customer(user)
    with db.transaction() as c:
        _conversation(c,user,cid)
        if 'product_id' in data:
            if data['product_id']:_product(c,data['product_id'])
            c.execute('UPDATE conversations SET selected_product_id=? WHERE id=?',(data['product_id'],cid))
        if 'order_id' in data:
            if data['order_id']:_order(c,user,data['order_id'])
            c.execute('UPDATE conversations SET selected_order_id=? WHERE id=?',(data['order_id'],cid))
        return _conversation(c,user,cid)


def _attachments(c,user,ids):
    if not isinstance(ids,list):fail(422,'图片列表格式不正确，请重新选择或上传图片')
    if len(ids)>3:fail(422,'每条消息最多上传三张图片，请分批发送')
    if any(not isinstance(aid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',aid) for aid in ids):
        fail(422,'图片标识格式不正确，请重新上传图片后再发送')
    if len(set(ids))!=len(ids):fail(422,'同一张图片不能重复添加，请移除重复图片后再发送')
    for aid in ids:
        row=one(c,'SELECT * FROM attachments WHERE id=?',(aid,),'图片不存在')
        if row['user_id']!=user['id']:fail(404,'图片不存在或不属于当前身份')
    return ids


def _new_turn(c,user,cid):
    current=c.execute("SELECT id FROM turns WHERE conversation_id=? AND status='pending'",(cid,)).fetchone()
    if current:fail(409,'上一条回复仍在处理中，请稍后再发送')
    conv=_conversation(c,user,cid)
    if conv['mode']!='ai':return None
    tid=db.uid('turn');stamp=db.now()
    c.execute('INSERT INTO turns(id,conversation_id,user_id,generation,phase,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',(tid,cid,conv['user_id'],conv['generation'],'正在理解你的问题',stamp,stamp))
    return tid


def _material_case_correction(text):
    """A new remedy or material fact makes an unsubmitted proposal unconfirmable.

    This runs before an AI turn: a later model assessment cannot protect the
    interval between the customer's message and the eventual draft update.
    """
    if not text:
        return False
    material = re.search(r'退货|退款|换货|补发|维修|损坏|摔|故障|质量|拆封|使用|配件|包装|签收|收到|缺件|漏发|少发|数量|(?:退|换|补)\s*[1-9一二两三]\s*件|不退了|不换了|不补发了|撤回申请|取消申请', text)
    correction = re.search(r'改成|改为|换成|不再|更正|补充|重新(?:判断|评估|核对|申请)|刚才.{0,15}(?:说错|不对)|其实|后来|不(?:想|要|打算)(?:退货|退款|换货|补发)|不退了|不换了|不补发了|撤回申请|取消申请', text)
    direct_fact = re.search(r'(?:我|自己|人为).{0,8}(?:摔|碰坏|弄坏|损坏)|(?:没|未|已经|已)(?:拆封|使用|收到)|(?:配件|包装).{0,10}(?:缺|少|不齐)|维修.{0,15}(?:失败|没修好|两次|2次)', text)
    return bool(material and (correction or direct_fact))


def _correction_requirements(text):
    requirements={}
    if re.search(r'(?:改成|改为|换成).{0,18}换货|不再(?:退货|退款).{0,30}换货',text):
        requirements['type']='exchange'
    elif re.search(r'(?:改成|改为|换成).{0,18}(?:退货|退款)|不再换货.{0,30}(?:退货|退款)',text):
        requirements['type']='return_refund'
    elif re.search(r'(?:改成|改为|换成).{0,18}补发',text):
        requirements['type']='reship'
    target=re.search(r'(?:改成|改为|换成)(.{0,36}?)(?:换货|补发|[，。！!]|$)',text)
    if target and re.search(r'换货|补发',text[max(0,target.start()-12):target.end()+6]):
        detail=target[1]
        color=re.search(r'曜石黑|鼠尾草绿|暖白|米白|燕麦色|白色|黑色|绿色|米色',detail)
        size=re.search(r'(?<![A-Za-z0-9])(?:XXL|XL|XS|[SML])(?![A-Za-z0-9])',detail,re.I)
        quantity=re.search(r'([1-9]\d?|一|两|二|三)\s*(?:件|个|台|套|副)',detail)
        if color:requirements['target_color']=color.group()
        if size:requirements['target_size']=size.group().upper()
        if quantity:requirements['quantity']={'一':1,'两':2,'二':2,'三':3}.get(quantity[1],int(quantity[1]) if quantity[1].isdigit() else None)
    if re.search(r'不再(?:退货|退款)|不(?:想|要|打算)(?:退货|退款)|取消(?:退货|退款)|不退了|撤回(?:退货|退款|申请)',text):
        requirements['forbid_type']='return_refund'
    elif re.search(r'不再换货|不(?:想|要|打算)换货|取消换货|不换了',text):
        requirements['forbid_type']='exchange'
    elif re.search(r'不再补发|不(?:想|要|打算)补发|取消补发|不补发了',text):
        requirements['forbid_type']='reship'
    if re.search(r'(?:没有|未|不是)维修失败|(?:仅|只)维修(?:了)?一次|维修(?:了)?一次',text):
        requirements['repair_status']='not_failed'
    elif re.search(r'维修.{0,20}(?:两次|2次|失败|没修好)|(?:两次|2次).{0,8}维修',text):
        requirements['repair_status']='failed'
    count=re.search(r'(?:改成|改为|更正|补充).{0,8}?(?:退|换|补)?\s*([1-9]\d?|一|两|二|三)\s*件',text)
    if count:
        value=count[1]
        requirements['quantity']={'一':1,'两':2,'二':2,'三':3}.get(value,int(value) if value.isdigit() else None)
    damage_denied=re.search(r'(?:不|没|没有|未|不是)(?:是)?(?:我|自己|人为).{0,8}(?:摔|碰坏|弄坏|损坏)|(?:我|自己).{0,8}(?:没|没有|未)(?:摔|碰坏|弄坏|损坏)',text)
    if damage_denied:
        requirements['customer_damage']=False
    elif re.search(r'(?:我|自己|人为).{0,8}(?:摔|碰坏|弄坏|损坏)',text):
        requirements['customer_damage']=True
    for tag,pattern in (
        ('unopened',r'未拆封|没拆封|没有拆封'),
        ('opened',r'已拆封|拆过封|拆封查验|仅拆封|只是拆封|拆封看了看'),
        ('unused',r'未使用|没用过|没有使用'),
        ('used',r'已使用|用过'),
        ('accessories_complete',r'配件(?:\s*(?:都|是|已经|仍然)?\s*(?:齐全|完整|没少)|\s*都在)'),
        ('accessories_missing',r'配件\s*(?:有|是|已经)?\s*(?:缺|少|不齐)'),
        ('packaging_complete',r'包装\s*(?:都|是|仍然)?\s*(?:齐全|完整|完好)'),
        ('packaging_missing',r'包装\s*(?:有|是)?\s*(?:缺|少|不齐|损坏)'),
    ):
        if re.search(pattern,text):requirements[tag]=True
    return requirements


def _draft_reflects_correction(c,before,case):
    """An explicit edit must actually incorporate the newer customer change."""
    if not any(before.get(key)!=case.get(key) for key in
               ('type','quantity','reason','condition_note','target_sku_id','attachment_ids','facts','reship_kind','part_sku_id')):
        return False
    requirements=before.get('stale_requirements') or {}
    if requirements.get('type') and case['type']!=requirements['type']:
        return False
    if case['type'] in ([requirements['forbid_type']] if requirements.get('forbid_type') else [])+requirements.get('additional_forbid_types',[]):
        return False
    if requirements.get('quantity') and case.get('quantity')!=requirements['quantity']:
        return False
    if requirements.get('target_color') or requirements.get('target_size'):
        sku_id=case.get('target_sku_id')
        if not sku_id:return False
        try:sku=_sku(c,sku_id)
        except HTTPException:return False
        color=requirements.get('target_color')
        if color and color not in (sku.get('color'),sku.get('label')):
            if not (color=='白色' and sku.get('color') in ('暖白','米白')):
                return False
        if requirements.get('target_size') and sku.get('size','').upper()!=requirements['target_size']:
            return False
    explanation=case.get('reason','')+'\n'+case.get('condition_note','')
    if requirements.get('type')=='exchange' and re.search(r'想退货|申请退货|要退款|想退款',explanation):
        return False
    if requirements.get('repair_status')=='failed' and not re.search(
            r'维修.{0,20}(?:两次|2次|失败|没修好)|(?:两次|2次).{0,8}维修',explanation):
        return False
    if requirements.get('repair_status')=='failed' and re.search(r'(?:没有|未|不是)维修失败',explanation):
        return False
    if requirements.get('repair_status')=='not_failed' and not re.search(
            r'(?:没有|未|不是)维修失败|(?:仅|只)维修(?:了)?一次|维修(?:了)?一次',explanation):
        return False
    if requirements.get('repair_status')=='not_failed':
        remaining=re.sub(r'(?:没有|未|不是)维修失败','',explanation)
        if re.search(r'维修.{0,8}(?:两次|2次|失败)|(?:两次|2次).{0,8}维修',remaining):
            return False
    damage_denied_pattern=r'(?:不|没|没有|未|不是)(?:是)?(?:我|自己|人为).{0,8}(?:摔|碰坏|弄坏|损坏)|(?:我|自己).{0,8}(?:没|没有|未)(?:摔|碰坏|弄坏|损坏)'
    damage_denied=re.search(damage_denied_pattern,explanation)
    positive_damage=re.search(r'(?:是我|我|自己|人为).{0,8}(?:摔|碰坏|弄坏|损坏)',re.sub(damage_denied_pattern,'',explanation))
    if requirements.get('customer_damage') is True and (not positive_damage or damage_denied):
        return False
    if requirements.get('customer_damage') is False and (not damage_denied or positive_damage):
        return False
    if requirements.get('customer_damage') is True:
        arrival_claim=re.sub(r'(?:不是|并非|没有)(?:到货|收到|开箱).{0,8}(?:坏|碎|损坏|破损)','',explanation)
        if re.search(r'(?:到货|收到|开箱).{0,8}(?:就坏|时坏|已坏|碎了|损坏|破损)',arrival_claim):
            return False
    if requirements.get('unopened') and re.search(r'已拆封|拆过封|拆封查验|仅拆封|只是拆封',explanation):
        return False
    if requirements.get('opened') and re.search(r'未拆封|没拆封|没有拆封',explanation):
        return False
    if requirements.get('unused') and re.search(r'已使用|用过',explanation):
        return False
    if requirements.get('used') and re.search(r'未使用|没用过|没有使用',explanation):
        return False
    if requirements.get('accessories_complete') and (not re.search(r'配件(?:\s*(?:都|是|已经|仍然)?\s*(?:齐全|完整|没少)|\s*都在)',explanation) or re.search(r'配件\s*(?:有|是|已经)?\s*(?:缺|少|不齐)',explanation)):
        return False
    if requirements.get('accessories_missing') and (not re.search(r'配件\s*(?:有|是|已经)?\s*(?:缺|少|不齐)',explanation) or re.search(r'配件\s*(?:都|是|已经|仍然)?\s*(?:齐全|完整|没少)',explanation)):
        return False
    for tag,pattern in (
        ('unopened',r'未拆封|没拆封|没有拆封'),
        ('opened',r'已拆封|拆过封|拆封查验|仅拆封|只是拆封|拆封看了看'),
        ('unused',r'未使用|没用过|没有使用'),
        ('used',r'已使用|用过'),
        ('packaging_complete',r'包装\s*(?:都|是|仍然)?\s*(?:齐全|完整|完好)'),
        ('packaging_missing',r'包装\s*(?:有|是)?\s*(?:缺|少|不齐|损坏)'),
    ):
        if requirements.get(tag) and not re.search(pattern,explanation):
            return False
    for message_id in before.get('stale_unparsed_message_ids',[]):
        row=c.execute('SELECT content FROM messages WHERE id=?',(message_id,)).fetchone()
        correction=re.sub(r'\s+','',row[0]) if row else ''
        if not correction or correction not in re.sub(r'\s+','',explanation):
            return False
        if re.search(r'更正|改成|改为|不是|并非|不再|其实',row[0]):
            old_reason=str(before.get('reason') or '').strip()
            old_condition=str(before.get('condition_note') or '').strip()
            if (case.get('reason')==before.get('reason') or case.get('condition_note')==before.get('condition_note')
                    or (old_reason and old_reason in case.get('reason',''))
                    or (old_condition and old_condition in case.get('condition_note',''))):
                return False
        elif case.get('condition_note')==before.get('condition_note'):
            return False
    return True


def _invalidate_changed_drafts(c,user,conv,content,message,turn_id):
    """Version and block affected drafts atomically with a customer correction."""
    if not _material_case_correction(content):
        return
    rows=c.execute("SELECT data FROM cases WHERE user_id=? AND conversation_id=? AND status IN ('draft','needs_info','submitted','approved')",
                   (user['id'],conv['id'])).fetchall()
    drafts=[json.loads(row[0]) for row in rows]
    if not drafts:
        return
    owned=[r[0] for r in c.execute('SELECT id FROM orders WHERE user_id=?',(user['id'],))]
    mentioned={oid for oid in owned if re.search(r'(?<![A-Za-z0-9_-])'+re.escape(oid)+r'(?![A-Za-z0-9_-])',content,re.I)}
    explicit_refs=set(re.findall(r'(?<![A-Za-z0-9_-])(?:DEMO-|EVAL-|order_)[A-Za-z0-9_-]+',content,re.I))
    if mentioned:
        order_ids=mentioned
    elif explicit_refs:
        return
    else:
        selected=conv.get('selected_order_id')
        draft_orders={case['order_id'] for case in drafts}
        order_ids=({selected} if selected in draft_orders else set()) if selected else draft_orders
    for case in drafts:
        if case['order_id'] not in order_ids:
            continue
        before=json.loads(js(case))
        case['version']+=1
        case['customer_confirmed_version']=None
        case['stale_since_message_id']=message['id']
        prior=case.get('stale_requirements') or {}
        latest=_correction_requirements(content)
        merged={**prior,**latest}
        for opposite in (('unopened','opened'),('unused','used'),('accessories_complete','accessories_missing'),('packaging_complete','packaging_missing')):
            for current,old in (opposite,(opposite[1],opposite[0])):
                if latest.get(current):merged.pop(old,None)
        if prior.get('forbid_type') and latest.get('forbid_type') and prior['forbid_type']!=latest['forbid_type']:
            merged['forbid_type']=prior['forbid_type']
            merged['additional_forbid_types']=list(dict.fromkeys(prior.get('additional_forbid_types',[])+[latest['forbid_type']]))
        case['stale_requirements']=merged
        if not latest:
            case['stale_unparsed_message_ids']=case.get('stale_unparsed_message_ids',[])+[message['id']]
        _case_history(case,'customer_correction',user,'顾客补充或更正售后诉求，请先更新申请摘要再确认',turn_id)
        _store_case(c,case)
        audit(c,user,'顾客更正售后申请',case['id'],before,case)


def send_message(user,cid,data):
    require_customer(user)
    req=request_key(data,'client_message_id')
    content=str(data.get('content','')).strip()
    if len(content)>12000:fail(422,'消息过长，请分成几条发送')
    ids=data.get('attachment_ids',[])
    if not content and not ids:fail(422,'写下一句话或上传一张图片吧')
    with db.transaction() as c:
        conv=_conversation(c,user,cid)
        cached=c.execute("SELECT * FROM messages WHERE conversation_id=? AND role='user' AND client_message_id=?",(cid,req)).fetchone()
        if cached:return {'message':_message(cached),'turn_id':cached['turn_id'],'_new':False}
        _attachments(c,user,ids)
        tid=_new_turn(c,user,cid)
        message=_add_message(c,cid,'user',content,ids,turn_id=tid,client_message_id=req)
        _invalidate_changed_drafts(c,user,conv,content,message,tid)
        from . import conversation_state
        conversation_state.safe_sync(c,_conversation(c,user,cid),_products(c))
        selected=_conversation(c,user,cid)['selected_order_id']
        mentioned=[r[0] for r in c.execute('SELECT id FROM orders WHERE user_id=?',(user['id'],)) if r[0].lower() in content.lower()]
        notice_order=mentioned[0] if len(mentioned)==1 else (selected if not mentioned else None)
        if notice_order:
            kind=_notice_type(content)
            if kind:_save_notice(c,user['id'],notice_order,kind,message['created_at'])
        return {'message':message,'turn_id':tid,'_new':bool(tid)}


def retry_turn(user,cid,data):
    require_customer(user);req=request_key(data)
    with db.transaction() as c:
        _conversation(c,user,cid)
        cached=memo(c,user,'retry:'+cid,req)
        if cached:return {**cached,'_new':False}
        old=one(c,'SELECT * FROM turns WHERE id=?',(data.get('turn_id'),))
        if old['conversation_id']!=cid or old['user_id']!=user['id']:fail(404,'回复不存在')
        if old['status'] not in ('failed','cancelled'):fail(409,'这条回复无需重试')
        latest=one(c,'SELECT id FROM turns WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1',(cid,))
        if latest[0]!=old['id']:fail(409,'请重试最新的失败回复')
        tid=_new_turn(c,user,cid)
        if not tid:fail(409,'当前会话由人工接待，不能重试智能回复')
        out=remember(c,user,'retry:'+cid,req,{'turn_id':tid})
        return {**out,'_new':True}


def _handoff(c,user,cid,reason=''):
    conv=_conversation(c,user,cid)
    mode='human' if user['role']=='staff' else 'waiting_human'
    if conv['mode']=='human' and user['role']=='customer':return conv
    if conv['mode']!=mode:
        if mode=='human' and conv['mode']=='waiting_human':
            # Taking over one conversation must not discard its other pending
            # obligations. Preserve verified anchors without assigning owners.
            from . import work_operations
            work_operations.preserve_handoff_items(c,user,cid)
        c.execute('UPDATE conversations SET mode=?,generation=generation+1,updated_at=? WHERE id=?',(mode,db.now(),cid))
        c.execute("UPDATE turns SET status='cancelled',phase='已转交人工',updated_at=? WHERE conversation_id=? AND status='pending'",(db.now(),cid))
        _add_message(c,cid,'system',('人工客服已接入。' if mode=='human' else '已为你联系人工客服，请稍候。') + (f' {reason}' if reason else ''))
        if mode=='waiting_human':
            c.execute("UPDATE service_intents SET status='handed_off',updated_at=? WHERE conversation_id=? AND status IN ('pending','needs_info')",(db.now(),cid))
        audit(c,user,'接管会话' if mode=='human' else '请求人工',cid,conv,{'mode':mode})
    return _conversation(c,user,cid)


def handoff(user,cid):
    with db.transaction() as c:return _handoff(c,user,cid)


def resume(user,cid,data=None):
    data=data or {}
    req=data.get('request_id')
    if req is not None:request_key(data)
    with db.transaction() as c:
        conv=_conversation(c,user,cid)
        if req:
            cached=memo(c,user,'resume:'+cid,req)
            if cached:return {**_conversation(c,user,cid),'turn_id':cached.get('turn_id'),'_new':False}
        if 'expected_generation' in data and data['expected_generation']!=conv['generation']:
            fail(409,'会话接待状态已更新，请刷新后再切换')
        tid=None
        if conv['mode']!='ai':
            c.execute("UPDATE conversations SET mode='ai',generation=generation+1,updated_at=? WHERE id=?",(db.now(),cid))
            c.execute("UPDATE turns SET status='cancelled',phase='已切换智能客服',updated_at=? WHERE conversation_id=? AND status='pending'",(db.now(),cid))
            # Resume the last customer question only if no completed staff/AI
            # answer follows it. Pure handoff requests must not loop back.
            last=c.execute("SELECT rowid,* FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid DESC LIMIT 1",(cid,)).fetchone()
            if last:
                answered=c.execute("SELECT 1 FROM messages WHERE conversation_id=? AND rowid>? AND role IN ('assistant','staff') AND status='completed' LIMIT 1",(cid,last['rowid'])).fetchone()
                pure_handoff=_pure_handoff_request(last['content'])
                if not answered and not pure_handoff:
                    tid=_new_turn(c,user,cid)
                    c.execute('INSERT INTO turn_origins VALUES (?,?)',(tid,'resume'))
            _add_message(c,cid,'system','已切回智能客服。'+('正在继续处理你最近的问题。' if tid else '可以继续咨询。'))
            audit(c,user,'交回智能客服',cid,conv,{'mode':'ai'})
        result={**_conversation(c,user,cid),'turn_id':tid,'_new':bool(tid)}
        if req:remember(c,user,'resume:'+cid,req,{'turn_id':tid})
        return result


def staff_message(user,cid,data):
    require_staff(user);req=request_key(data,'client_message_id');content=str(data.get('content','')).strip()
    if not content or len(content)>12000:fail(422,'请填写 1–12000 字的回复')
    with db.transaction() as c:
        conv=_conversation(c,user,cid)
        old=c.execute("SELECT * FROM messages WHERE conversation_id=? AND role='staff' AND client_message_id=?",(cid,req)).fetchone()
        if old:return _message(old)
        if conv['mode']!='human':fail(409,'请先接管这段会话再回复')
        return _add_message(c,cid,'staff',content,client_message_id=req)


def _case(c,user,case_id):
    row=one(c,'SELECT * FROM cases WHERE id=?',(case_id,),'售后申请不存在')
    owner(user,row['user_id']);value=json.loads(row['data'])
    value['order']=_order(c,user,value['order_id'])
    value['target_sku']=_sku(c,value['target_sku_id']) if value['target_sku_id'] else None
    value['part_sku']=dict(one(c,'SELECT * FROM part_skus WHERE id=?',(value['part_sku_id'],))) if value.get('part_sku_id') else None
    return value


def _store_case(c,case):
    core={k:v for k,v in case.items() if k not in ('order','target_sku','part_sku')}
    c.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,version=excluded.version,data=excluded.data',(core['id'],core['user_id'],core['conversation_id'],core['order_id'],core['status'],core['version'],js(core)))


def _case_history(case,action,actor,note,turn_id=None):
    entry={'action':action,'actor':actor['name'],'note':note,'version':case['version'],'created_at':db.now()}
    if turn_id:entry['turn_id']=turn_id
    case['history'].append(entry)
    case['updated_at']=db.now()


def _expected(case,data):
    if data.get('expected_version')!=case['version']:fail(409,'申请已更新，请刷新后再操作')


def _wants_return(text):
    cleaned=re.sub(r'(不想|不要|不打算|不需要|不申请|无需|暂不).{0,6}(退货|退款)', '', text)
    return bool(re.search(r'(要|想|申请|帮我|给我|需要|打算).{0,10}(退货|退款)|退货申请|申请退|^(退货|退款)[吧。！! ]*$',cleaned))


def _notice_type(text):
    if _wants_return(text):return 'return_refund'
    clean=re.sub(r'(不想|不要|不打算|不需要|不申请|无需|暂不).{0,6}(补发|换货|换个|换成)', '', text)
    if re.search(r'(?:要|想|申请|需要|帮我).{0,8}补发|^补发[吧。！! ]*$',clean):return 'reship'
    if re.search(r'(?:要|想|申请|需要|帮我).{0,8}换(?:货|个|一|成)|^换货[吧。！! ]*$',clean):return 'exchange'
    return None


def _pure_handoff_request(text):
    if not re.search(r'人工|真人',text):return False
    # Remove communication preferences, not business questions. Unknown
    # remaining text is conservatively treated as something to answer.
    rest=re.sub(r'(?:不用|不要|不需要|无需|别)(?:再)?(?:追问|询问|问)(?:我)?(?:任何)?(?:原因|理由|问题)?(?:了)?','',text)
    rest=re.sub(r'(?:我)?不想(?:再)?(?:跟|和|与)?(?:机器人|智能客服|AI)(?:聊|沟通|说话)(?:了)?','',rest,flags=re.I)
    rest=re.sub(r'(?:别|不要)(?:再)?(?:让我|叫我)?(?:说明|解释)(?:原因|理由)','',rest)
    rest=re.sub(r'[，。？！!?；;,\s]','',rest)
    return bool(re.fullmatch(r'(?:请你|帮我|麻烦|请|我|想|要|需要|给我|现在|立刻|马上|直接|劳驾|转接|转|接|找|联系|人工|客服|真人|服务|接入|吧|谢谢|多谢|了|一下|好吗|可以吗|能不能|行吗)+',rest))


def _notice_floor(c,user_id,order_id,kind):
    stamps=[]
    for row in c.execute("SELECT data FROM cases WHERE user_id=? AND order_id=? AND status IN ('rejected','completed')",(user_id,order_id)):
        case=json.loads(row[0])
        if case['type']==kind:stamps.append(case['updated_at'])
    return max(stamps) if stamps else None


def _eligible_notice(c,user_id,order_id,kind):
    row=c.execute('SELECT first_at FROM case_notices WHERE user_id=? AND order_id=? AND type=?',(user_id,order_id,kind)).fetchone()
    if not row and kind=='return_refund':row=c.execute('SELECT first_at FROM return_notices WHERE user_id=? AND order_id=?',(user_id,order_id)).fetchone()
    floor=_notice_floor(c,user_id,order_id,kind)
    return row[0] if row and (floor is None or row[0]>floor) else None


def _save_notice(c,user_id,order_id,kind,stamp):
    first=_eligible_notice(c,user_id,order_id,kind) or stamp
    c.execute('INSERT INTO case_notices VALUES (?,?,?,?) ON CONFLICT(user_id,order_id,type) DO UPDATE SET first_at=excluded.first_at',(user_id,order_id,kind,first))
    if kind=='return_refund':
        c.execute('INSERT INTO return_notices VALUES (?,?,?) ON CONFLICT(user_id,order_id) DO UPDATE SET first_at=excluded.first_at',(user_id,order_id,first))
    return first


def _first_type_notice(c,conv,order,kind):
    existing=_eligible_notice(c,conv['user_id'],order['id'],kind)
    if existing:return existing
    floor=_notice_floor(c,conv['user_id'],order['id'],kind)
    first=db.now()
    for message in c.execute("SELECT content,created_at FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid",(conv['id'],)):
        if _notice_type(message['content'])!=kind:continue
        if floor and message['created_at']<=floor:continue
        used=c.execute('SELECT 1 FROM case_notices WHERE user_id=? AND order_id!=? AND first_at=?',(conv['user_id'],order['id'],message['created_at'])).fetchone()
        if not used:
            first=message['created_at'];break
    return first


def _first_notice(c,cid,reason,oid=None):
    conv=one(c,'SELECT user_id FROM conversations WHERE id=?',(cid,))
    stamp=_first_type_notice(c,{'id':cid,'user_id':conv['user_id']},{'id':oid},'return_refund')
    return _save_notice(c,conv['user_id'],oid,'return_refund',stamp)


def _available_quantity(c,order,request,exclude_id=None):
    part=request.get('reship_kind')=='part'
    if part:
        entry=next((x for x in order.get('package_snapshot',[]) if x.get('part_sku_id')==request.get('part_sku_id')),None)
        if not entry:return 0
        total=order['quantity']*entry.get('quantity',1)
    else:total=order['quantity']
    for row in c.execute("SELECT id,data FROM cases WHERE order_id=? AND status IN ('submitted','needs_info','approved','completed')",(order['id'],)):
        if row['id']==exclude_id:continue
        previous=json.loads(row['data'])
        if part:
            if previous.get('reship_kind')=='part' and previous.get('part_sku_id')==request.get('part_sku_id'):
                total-=previous['quantity']
            elif previous.get('reship_kind')!='part':
                # Returned/replaced units no longer have a separate missing-part entitlement.
                total-=previous['quantity']*entry.get('quantity',1)
        elif previous.get('reship_kind')!='part':total-=previous['quantity']
    return max(0,total)


def _evaluate(c,user,data,order=None,stored_case=False):
    order=order or _order(c,user,data.get('order_id'))
    product=_product(c,order['product_id'])
    original=_policy(c,order['policy_version']);current=_policy(c)
    rules=policy_engine.effective_policy(original,current)
    raw=data.get('facts')
    if stored_case and isinstance(raw,dict):
        # Server-derived counts must be recomputed from the current purchase
        # snapshot, never recycled as a purported customer statement.
        raw={key:item for key,item in raw.items() if not isinstance(item,dict) or item.get('source')!='order_inference'}
    facts=case_facts.merge(str(data.get('reason','')),str(data.get('condition_note','')),raw,order_id=order['id'])
    text='\n'.join([str(data.get('reason','')),str(data.get('condition_note',''))])
    facts=case_facts.apply_conflict_guard(facts,text)
    # Quoted evidence can expose a conflict omitted from a short summary. Do
    # not use arbitrary dictionary order to treat an old quote as a correction.
    if 'causation_conflict' not in facts:
        for item in list(facts.values()):
            if isinstance(item,dict) and item.get('quote'):
                guarded=case_facts.apply_conflict_guard(facts,item['quote'])
                if case_facts.values(guarded).get('causation_conflict') is True:
                    facts=guarded;break
    target=None;part=None
    sid=data.get('target_sku_id') or (order['sku_id'] if data.get('type')=='reship' else None)
    if sid:
        row=c.execute('SELECT * FROM skus WHERE id=?',(sid,)).fetchone()
        target=dict(row) if row else None
    if data.get('part_sku_id'):
        part=next((p for p in _parts(c,order) if p['id']==data['part_sku_id']),None)
    f=case_facts.values(facts)
    expected_quantity=order['quantity']*(part.get('included_quantity',1) if part else 1)
    if (data.get('type')=='reship' and f.get('issue_type')=='missing' and 'missing_quantity' not in f
            and f.get('responsibility')!='customer' and not f.get('causation_conflict')):
        derived=None;basis=None
        if 'received_quantity' in f and 0<=f['received_quantity']<=expected_quantity:
            derived=expected_quantity-f['received_quantity']
            basis='订单应含数量减去顾客明确实收数量'
        elif data.get('reship_kind')=='part' and part and order['quantity']==1 and part.get('included_quantity')==1:
            derived=1;basis='单件订单包装清单只含该配件一个，顾客已明确该配件欠发'
        if derived is not None:
            facts['missing_quantity']={'value':derived,'source':'order_inference','order_id':order['id'],
                'part_sku_id':part['id'] if part else None,'expected_quantity':expected_quantity,'basis':basis}
    first=data.get('first_notice_at')
    if not first:
        first=_eligible_notice(c,order['user_id'],order['id'],data.get('type')) or db.now()
    request={**data,'first_notice_at':first,'original_price_cents':_sku(c,order['sku_id'])['price_cents'],'expected_component_quantity':expected_quantity}
    # A replacement restarts quality coverage, while no-reason time remains tied
    # to the original received unit. Truly missing goods start when first received.
    assessed_order=order
    fact_values=case_facts.values(facts)
    if order.get('original_delivered_at') and (fact_values.get('issue_type')=='no_reason'
            or policy_engine.no_reason_eligibility_without_preference(data.get('type'),fact_values)):
        assessed_order={**order,'delivered_at':order['original_delivered_at']}
    available=_available_quantity(c,order,request,data.get('id'))
    # Packaging eligibility is a clearer rejection than a resulting zero limit.
    quantity_limit=None if data.get('reship_kind')=='part' and not part else available
    assessment=policy_engine.assess(assessed_order,product,request,rules,facts,now=db.now(),target=target,part=part,available_quantity=quantity_limit)
    # assess() owns both the dynamic decision and its displayed rule titles.
    qty=data.get('quantity',1)
    refund=0
    if data.get('type')=='return_refund' and isinstance(qty,int) and not isinstance(qty,bool) and qty>0:
        _,refund=_case_refund(c,{'id':data.get('id',''),'quantity':qty,'type':'return_refund'},order)
    return {'assessment':assessment,'facts':facts,'order_id':order['id'],'type':data.get('type'),'quantity':qty,
            'refund_cents':refund,'shipping_payer':assessment['shipping_payer'],'target_sku_id':sid,
            'reship_kind':data.get('reship_kind','whole'),'part_sku_id':data.get('part_sku_id'),
            'policy_snapshot':original,'assessment_policy':rules,'first_notice_at':first}


def evaluate_after_sale(user,data,guard_turn=None,*,update_draft=True,draft_target=None):
    require_customer(user)
    if not isinstance(data,dict):fail(422,'申请信息格式不正确')
    if data.get('type') not in ('return_refund','exchange','reship','refund_only','repair','logistics'):fail(422,'请选择有效售后类型')
    data={k:v for k,v in data.items() if k in ('order_id','type','quantity','reason','condition_note','facts','target_sku_id','reship_kind','part_sku_id','parent_case_id')}
    try:
        if 'facts' in data:data['facts']=case_facts.normalize(data['facts'])
        # A chat evaluation can correct a previously displayed draft. Keep the
        # turn guard, revision read and any correction in one write transaction
        # so a staff handoff/submission cannot race an obsolete AI conclusion.
        with (db.transaction() if guard_turn else db.connect()) as c:
            turn=_current(c,guard_turn) if guard_turn else None
            if guard_turn and not turn:fail(409,'回复已失效，不能生成旧建议')
            old=None
            if draft_target is not None:
                if not turn or not update_draft:fail(409,'指定草稿更新必须属于有效的顾客回复')
                target_id,target_version=draft_target
                old=_case(c,user,target_id)
                if (old['conversation_id']!=turn['conversation_id'] or old['order_id']!=data.get('order_id')
                        or old['type']!=data.get('type') or old['status']!='draft'):
                    fail(409,'指定申请已变化，不能更新其他申请或另建草稿')
                _expected(old,{'expected_version':target_version})
            elif turn and update_draft:
                row=c.execute("SELECT id FROM cases WHERE conversation_id=? AND order_id=? AND user_id=? AND status='draft' ORDER BY rowid DESC",(turn['conversation_id'],data.get('order_id'),user['id'])).fetchall()
                for item in row:
                    candidate=_case(c,user,item['id'])
                    if candidate['type']==data['type']:
                        old=candidate;break
            if old is None:return _evaluate(c,user,data)
            if old['updated_at']>turn['created_at'] and (old.get('history') or [{}])[-1].get('turn_id')!=guard_turn:
                fail(409,'申请在本次回复期间已更新，请基于最新申请重新确认；旧回复不会覆盖新草稿')
            candidate={**old,**data}
            if 'facts' not in data and any(key in data for key in ('reason','condition_note')):
                candidate['facts']=None
            evaluated=_evaluate(c,user,candidate,stored_case=True)
            # The evidence is part of the reviewable record. A new quotation or
            # condition note must survive even when its normalized value and
            # eligibility decision are unchanged.
            changed=((old.get('facts') or {})!=evaluated['facts'] or
                any(candidate.get(key)!=old.get(key) for key in ('quantity','target_sku_id','reship_kind','part_sku_id','reason','condition_note')))
            current=old
            if changed:
                # Pass only customer statements through the public validator;
                # derived counts are recomputed during _assess_case.
                facts={key:value for key,value in evaluated['facts'].items() if value.get('source')!='order_inference'}
                edits={key:candidate.get(key) for key in ('quantity','reason','condition_note','target_sku_id','reship_kind','part_sku_id')}
                current=_patch_case(c,user,old['id'],{**edits,'facts':facts,'expected_version':old['version']},guard_turn=guard_turn)
                evaluated.update({key:current.get(key) for key in ('assessment','facts','refund_cents','shipping_payer','first_notice_at','quantity','target_sku_id','reship_kind','part_sku_id')})
            return {**evaluated,'case_id':current['id'],'case_version':current['version'],'case':current,'case_updated':changed}
    except ValueError as exc:fail(422,str(exc))


def _assess_case(c,case,order):
    user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(case['user_id'],))[0])
    evaluated=_evaluate(c,user,case,order,stored_case=True)
    old=case.get('assessment')
    if old:case.setdefault('assessment_history',[]).append(old)
    for key in ('assessment','facts','shipping_payer','policy_snapshot','assessment_policy'):
        case[key]=evaluated[key]
    assessment=case['assessment']
    case['suggestion']=assessment['headline']+'：'+'；'.join(assessment['reasons'])
    case['policy_basis']=assessment['policy_basis']
    indices,refund=_case_refund(c,case,order)
    case['unit_indices']=indices
    case['refund_cents']=refund


def apply_assessment_result(user,case_id,expected_version,facts,job_id=None,job_result=None,job_usage=None):
    """Apply a background extraction only to the exact still-editable revision."""
    with db.transaction() as c:
        if job_id:
            job=one(c,'SELECT * FROM assessments WHERE id=?',(job_id,),'评估任务不存在')
            owner(user,job['user_id'])
            payload=json.loads(job['payload'])
            if payload.get('case_id')!=case_id or payload.get('case_version')!=expected_version:
                fail(409,'评估任务与申请版本不匹配')
            if job['status']!='pending':return None
        case=_case(c,user,case_id)
        if case['version']!=expected_version or case['status'] not in ('draft','needs_info','submitted'):
            if job_id:
                c.execute("UPDATE assessments SET status='superseded',result=?,usage=?,updated_at=? WHERE id=? AND status='pending'",(js(job_result or {}),js(job_usage or {}),db.now(),job_id))
            return None
        before=json.loads(js(case))
        try:case['facts']=case_facts.normalize(facts)
        except ValueError as exc:fail(422,str(exc))
        order=_validate_case(c,user,case,case_id)
        _assess_case(c,case,order)
        case['version']+=1
        if policy_engine.proposal_changed(before,case):
            case['status']='draft';case['customer_confirmed_version']=None
        elif before.get('customer_confirmed_version')==expected_version:
            case['customer_confirmed_version']=case['version']
        _case_history(case,'assessment',user,'根据已补充事实更新售后建议')
        _store_case(c,case)
        updated=_case(c,user,case_id)
        if job_id:
            result=dict(job_result or {})
            result.update({key:updated.get(key) for key in ('assessment','facts','refund_cents','shipping_payer','first_notice_at','order_id','type','quantity','target_sku_id','reship_kind','part_sku_id')})
            c.execute("UPDATE assessments SET status='completed',result=?,usage=?,updated_at=? WHERE id=? AND status='pending'",(js(result),js(job_usage or {}),db.now(),job_id))
        return updated


def refresh_legacy_assessments(connection=None):
    """Upgrade advice only on unreviewed cases; never overwrite a staff outcome."""
    with (db.transaction() if connection is None else nullcontext(connection)) as c:
        current=_policy(c)
        for row in c.execute("SELECT data FROM cases WHERE status IN ('draft','submitted','needs_info')").fetchall():
            case=json.loads(row[0])
            original=case.get('policy_snapshot') or current
            if case.get('assessment',{}).get('policy_fingerprint')==policy_engine.effective_policy(original,current)['fingerprint']:continue
            user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(case['user_id'],))[0])
            before=json.loads(js(case))
            order=_order(c,user,case['order_id'])
            _assess_case(c,case,order)
            case['version']+=1
            if case.get('customer_confirmed_version') is not None:
                if policy_engine.proposal_changed(before,case):
                    case['customer_confirmed_version']=None;case['status']='draft'
                else:case['customer_confirmed_version']=case['version']
            _case_history(case,'policy_upgrade',user,'保留原规则快照，按更有利店铺承诺重新给出建议；人工结果不变')
            _store_case(c,case)
            audit(c,{'id':'system'},'更新未终审建议',case['id'],before,case)


def _case_refund(c,case,order):
    if case.get('reship_kind')=='part':return [],0
    occupied=set()
    for row in c.execute("SELECT data FROM cases WHERE order_id=? AND id!=? AND status IN ('submitted','needs_info','approved','completed') ORDER BY rowid",(order['id'],case['id'])):
        prior=json.loads(row[0])
        if prior.get('reship_kind')=='part':continue
        indices=prior.get('unit_indices')
        if indices is None:
            indices=[i for i in range(order['quantity']) if i not in occupied][:prior['quantity']]
        occupied.update(indices)
    indices=[i for i in range(order['quantity']) if i not in occupied][:case['quantity']]
    unit,remainder=divmod(order['total_cents'],order['quantity'])
    refund=sum(unit+(i<remainder) for i in indices) if case['type']=='return_refund' else 0
    return indices,refund


def _validate_case(c,user,case,exclude_id=None):
    order=_order(c,user,case['order_id'])
    if order['status']!='delivered':fail(409,'请在订单签收后申请；未签收问题可联系人工')
    positive(case['quantity'])
    if case['quantity']>_available_quantity(c,order,case,exclude_id):fail(409,'申请数量超过订单尚可售后的商品或配件数量')
    if case['type'] not in ('return_refund','exchange','reship'):fail(422,'请选择退货退款、换货或补发')
    if not case['reason'].strip() or len(case['reason'])>4000:fail(422,'请填写 1–4000 字的申请原因')
    if len(case['condition_note'])>4000:fail(422,'商品状况说明过长')
    # Attachment owner is always the customer, even for an amendment made by staff.
    customer=json.loads(one(c,'SELECT data FROM users WHERE id=?',(order['user_id'],))[0])
    _attachments(c,customer,case['attachment_ids'])
    if case.get('reship_kind','whole') not in ('whole','part'):fail(422,'请选择整件或配件补发')
    if case.get('reship_kind')=='part':
        if case['type']!='reship':fail(422,'配件范围仅用于补发')
        if not case.get('part_sku_id'):fail(422,'请选择包装清单中的具体配件')
        if not any(p['id']==case['part_sku_id'] for p in _parts(c,order)):fail(422,'该配件不属于订单购买时包装清单')
        case['target_sku_id']=None
        return order
    case['part_sku_id']=None
    if case['type']=='return_refund':
        case['target_sku_id']=None
    else:
        target=case.get('target_sku_id') or (order['sku_id'] if case['type']=='reship' else None)
        if not target:fail(422,'请先选择需要更换的规格')
        sku=_sku(c,target)
        if sku['product_id']!=order['product_id']:fail(422,'第一版仅支持同款商品的规格更换或补发')
        original=_sku(c,order['sku_id'])
        if sku['price_cents']!=original['price_cents']:fail(422,'这个规格存在价差，请联系人工处理')
        if case['type']=='reship' and target!=order['sku_id']:fail(422,'补发应使用原订单规格；更换规格请选择换货')
        case['target_sku_id']=target
    return order


def public_case_input(data, *, reset_facts=False):
    """HTTP form text is awaiting extraction, never a trusted model fact set.

    Preserve existing validated facts for quantity/target-only edits. New text,
    evidence or request kind invalidates them; the assessment job will replace
    the empty set only for the exact current revision.
    """
    result={key:value for key,value in data.items() if key!='facts'}
    if reset_facts or any(key in result for key in ('reason','condition_note','attachment_ids','type')):
        result['facts']={}
    return result


def create_case(user,data,guard_turn=None):
    require_customer(user);req=request_key(data)
    with db.transaction() as c:
        if guard_turn and not _current(c,guard_turn):fail(409,'人工已接管，申请草稿未创建')
        cached=memo(c,user,'case:create',req)
        if cached:return _case(c,user,cached['id'])
        conv=_conversation(c,user,data.get('conversation_id'))
        order=_order(c,user,data.get('order_id'))
        parent_id=data.get('parent_case_id');parent=None
        if parent_id:
            parent=_case(c,user,parent_id)
            if parent['order_id']!=order['id'] and parent.get('replacement_order_id')!=order['id']:fail(422,'关联申请不属于本订单')
            if parent.get('replacement_order_id') and parent['order_id']==order['id']:
                order=_order(c,user,parent['replacement_order_id'])
        if order['user_id']!=conv['user_id']:fail(404,'该订单不属于会话顾客')
        stamp=db.now()
        case={'id':db.uid('case'),'user_id':user['id'],'conversation_id':conv['id'],'order_id':order['id'],'type':data.get('type'),'status':'draft','quantity':data.get('quantity'),'target_sku_id':data.get('target_sku_id'),'reason':str(data.get('reason','')).strip(),'condition_note':str(data.get('condition_note','')).strip(),'attachment_ids':data.get('attachment_ids',[]),'suggestion':'','policy_basis':[],'refund_cents':0,'shipping_payer':'pending','first_notice_at':(_first_notice(c,conv['id'],data.get('reason',''),order['id']) if data.get('type')=='return_refund' else stamp),'version':1,'customer_confirmed_version':None,'review_note':'','completion_note':'','created_at':stamp,'updated_at':stamp,'history':[]}
        try:case['facts']=case_facts.normalize(data.get('facts')) if 'facts' in data else None
        except ValueError as exc:fail(422,str(exc))
        case.update(reship_kind=data.get('reship_kind','whole'),part_sku_id=data.get('part_sku_id'),parent_case_id=parent_id)
        if case['type']!='return_refund':case['first_notice_at']=_first_type_notice(c,conv,order,case['type'])
        previous=_eligible_notice(c,user['id'],order['id'],case['type'])
        if previous:case['first_notice_at']=previous
        if parent and parent['order_id']==order['id'] and parent['type']==case['type']:
            case['first_notice_at']=parent['first_notice_at']
        else:_save_notice(c,user['id'],order['id'],case['type'],case['first_notice_at'])
        _validate_case(c,user,case)
        _assess_case(c,case,order)
        case['intent_ids']=[]
        from . import conversation_state
        for intent_row in c.execute("SELECT id,data FROM service_intents WHERE conversation_id=? AND status IN ('pending','needs_info','handed_off')",(conv['id'],)):
            intent=json.loads(intent_row['data'])
            types=[intent['request_type']] if intent.get('request_type') else conversation_state.request_types(intent.get('text',''))
            if (intent.get('kind')=='after_sale' and order['id'] in intent.get('order_ids',[])
                    and types == [case['type']]):case['intent_ids'].append(intent_row['id'])
        _case_history(case,'draft',user,'整理售后申请，等待顾客确认',guard_turn)
        _store_case(c,case)
        remember(c,user,'case:create',req,{'id':case['id']})
        return _case(c,user,case['id'])


def get_cases(user,order_id=None):
    with db.connect() as c:
        rows=c.execute('SELECT id FROM cases ORDER BY rowid DESC') if user['role']=='staff' else c.execute('SELECT id FROM cases WHERE user_id=? ORDER BY rowid DESC',(user['id'],))
        result=[_case(c,user,r[0]) for r in rows]
        return [v for v in result if v['order_id']==order_id] if order_id else result


def patch_case(user,case_id,data,guard_turn=None,agent_request=None):
    with db.transaction() as c:
        return _patch_case(c,user,case_id,data,guard_turn,agent_request)


def _patch_case(c,user,case_id,data,guard_turn=None,agent_request=None):
    turn=_current(c,guard_turn) if guard_turn else None
    if guard_turn and not turn:fail(409,'人工已接管，草稿未修改')
    if agent_request:
        cached=memo(c,user,'agentdraft',agent_request)
        if cached:return _case(c,user,cached['id'])
    case=_case(c,user,case_id);_expected(case,data)
    if guard_turn and case['status']!='draft':fail(409,'申请已提交，不能由智能客服修改')
    if turn and case['updated_at']>turn['created_at'] and (case.get('history') or [{}])[-1].get('turn_id')!=guard_turn:
        fail(409,'申请在本次回复期间已更新，旧回复不能覆盖新草稿')
    if user['role']=='customer' and case['status'] not in ('draft','needs_info'):fail(409,'当前状态不能修改申请，请联系人工')
    if user['role']=='staff' and case['status'] in ('completed','rejected'):fail(409,'已经结束的申请不能修改')
    before=json.loads(js(case))
    editable=['type','quantity','reason','condition_note','target_sku_id','attachment_ids','reship_kind','part_sku_id']
    for key in editable:
        if key in data:case[key]=data[key]
    if not isinstance(case['reason'],str) or not isinstance(case['condition_note'],str):fail(422,'原因与商品状况应为文字')
    if 'facts' in data:
        try:case['facts']=case_facts.normalize(data['facts'])
        except ValueError as exc:fail(422,str(exc))
    elif any(key in data for key in ('reason','condition_note')):
        # Revised text must not be overruled by an obsolete extraction.
        case['facts']=None
    if case['type']!=before['type']:
        case['first_notice_at']=_eligible_notice(c,case['user_id'],case['order_id'],case['type']) or db.now()
        _save_notice(c,case['user_id'],case['order_id'],case['type'],case['first_notice_at'])
    order=_validate_case(c,user,case,case['id'])
    case['version']+=1;case['customer_confirmed_version']=None;case['status']='draft'
    _assess_case(c,case,order)
    if user['role']=='staff' and 'shipping_payer' in data:
        if data['shipping_payer'] not in ('customer','store','pending'):fail(422,'请选择有效运费承担方')
        case['shipping_payer']=data['shipping_payer']
    if before.get('stale_since_message_id') and not _draft_reflects_correction(c,before,case):
        fail(409,'申请摘要尚未反映顾客最新更正，请更新事实或申请类型后再确认')
    case['stale_since_message_id']=None
    case['stale_requirements']=None
    case['stale_unparsed_message_ids']=[]
    _case_history(case,'amend',user,'申请方案已更新，请顾客重新确认',guard_turn)
    _store_case(c,case);audit(c,user,'修改售后方案',case_id,before,case)
    if agent_request:remember(c,user,'agentdraft',agent_request,{'id':case_id})
    _add_message(c,case['conversation_id'],'system','售后方案已更新，请确认新的申请摘要。',cards=[{'type':'case','data':_case(c,user,case_id)}])
    return _case(c,user,case_id)

def _record_fulfillment(c,case):
    stamp=db.now()
    fulfillment={'id':db.uid('fulfill'),'case_id':case['id'],'order_id':case['order_id'],
                 'reship_kind':case.get('reship_kind','whole'),'quantity':case['quantity'],
                 'sku_id':case.get('part_sku_id') or case['target_sku_id'],'delivered_at':stamp,
                 'return_handling':case.get('return_handling'),'return_note':case.get('return_note')}
    if case.get('reship_kind')!='part':
        user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(case['user_id'],))[0])
        original=_order(c,user,case['order_id']);sku=_sku(c,case['target_sku_id'])
        per,extra=divmod(original['total_cents'],original['quantity'])
        total=sum(per+(i<extra) for i in case.get('unit_indices',range(case['quantity'])))
        replacement={**original,'id':db.uid('order'),'sku_id':sku['id'],'sku_label':sku['label'],
                     'quantity':case['quantity'],'total_cents':total,
                     'discount_cents':original['unit_price_cents']*case['quantity']-total,
                     'created_at':stamp,'delivered_at':stamp,'remaining_quantity':case['quantity'],
                     'parent_order_id':original['id'],'parent_case_id':case['id'],
                     'fulfillment_kind':case['type'],'warranty_started_at':stamp}
        issue=case_facts.values(case.get('facts',{})).get('issue_type')
        if not (case['type']=='reship' and issue=='missing'):
            replacement['original_delivered_at']=original.get('original_delivered_at',original['delivered_at'])
        else:replacement.pop('original_delivered_at',None)
        c.execute('INSERT INTO orders VALUES (?,?,?)',(replacement['id'],case['user_id'],js(replacement)))
        case['replacement_order_id']=replacement['id']
        fulfillment['replacement_order_id']=replacement['id']
    c.execute('INSERT INTO fulfillments VALUES (?,?,?,?,?)',(fulfillment['id'],case['id'],case['order_id'],case['user_id'],js(fulfillment)))
    case['fulfillment']=fulfillment


def _case_action(user,case_id,data,action):
    req=request_key(data)
    if action in ('review','complete'):require_staff(user)
    else:require_customer(user)
    with db.transaction() as c:
        cached=memo(c,user,f'case:{case_id}:{action}',req)
        if cached:return _case(c,user,case_id)
        case=_case(c,user,case_id);_expected(case,data);before=json.loads(js(case))
        if action=='submit':
            if case['status']!='draft':fail(409,'只有待确认草稿可以提交')
            if case.get('stale_since_message_id'):fail(409,'顾客已补充或更正诉求，请先更新申请摘要再确认')
            order=_validate_case(c,user,case,case_id)
            indices,refund=_case_refund(c,case,order)
            case['unit_indices']=indices
            if refund!=case['refund_cents']:
                case['refund_cents']=refund;case['version']+=1;case['customer_confirmed_version']=None
                _case_history(case,'refund_updated',user,'同订单的其他申请已改变优惠分摊，请重新确认退款金额')
                _store_case(c,case)
                audit(c,user,'重新分摊退款',case_id,before,case)
                return {'_reconfirm':True}
            case['status']='submitted';case['version']+=1;case['customer_confirmed_version']=case['version']
            note='顾客已确认申请，等待人工审核'
        elif action=='supplements':
            if case['status']!='needs_info':fail(409,'当前申请不需要补充材料')
            content=str(data.get('content','')).strip();ids=data.get('attachment_ids',[])
            if not content and not ids:fail(422,'请填写补充说明或上传图片')
            _attachments(c,user,ids)
            # Keep prior evidence plus new evidence in history, while latest set is capped per message.
            case['condition_note']+='\n补充说明：'+content
            try:case['facts']=case_facts.normalize(data['facts']) if 'facts' in data else None
            except ValueError as exc:fail(422,str(exc))
            if ids:case['attachment_ids']=list(dict.fromkeys(case['attachment_ids']+ids))[-3:]
            order=_validate_case(c,user,case,case_id)
            _assess_case(c,case,order)
            if before.get('stale_since_message_id'):
                if not _draft_reflects_correction(c,before,case):
                    fail(409,'补充材料尚未反映顾客最新更正，请更新申请后再确认')
                case['stale_since_message_id']=None
                case['stale_requirements']=None
                case['stale_unparsed_message_ids']=[]
            case['status']='submitted';case['version']+=1;case['customer_confirmed_version']=case['version']
            if before.get('shipping_payer')!=case['shipping_payer'] or before.get('refund_cents')!=case['refund_cents']:
                case['status']='draft';case['customer_confirmed_version']=None
            note=content or '已补充图片'
            _add_message(c,case['conversation_id'],'user',f'补充售后材料：{note}',ids)
        elif action=='review':
            if case['status']!='submitted':fail(409,'只有待审核申请可以审核')
            note=str(data.get('note','')).strip()
            if not note:fail(422,'请填写顾客能理解的审核说明')
            decision=data.get('decision')
            state={'approve':'approved','reject':'rejected','needs_info':'needs_info'}.get(decision)
            if not state:fail(422,'请选择有效的审核结果')
            if case.get('stale_since_message_id') and decision=='approve':
                fail(409,'顾客已更正或撤回旧申请，请先核对并更新方案')
            stale_requirements=case.get('stale_requirements') or {}
            withdrawn_types=[stale_requirements.get('forbid_type')]+stale_requirements.get('additional_forbid_types',[])
            if case.get('stale_since_message_id') and decision=='reject' and case['type'] not in withdrawn_types:
                fail(409,'顾客已更正事实，请先补充核对或更新方案')
            if case['customer_confirmed_version']!=case['version'] and not (case.get('stale_since_message_id') and (decision=='needs_info' or (decision=='reject' and case['type'] in withdrawn_types))):
                fail(409,'当前方案还未经顾客确认')
            _validate_case(c,user,case,case_id)
            if data.get('return_handling') in ('returned','scheduled','waived'):
                case['return_handling']=data['return_handling'];case['return_note']=str(data.get('return_note','')).strip()
            case['status']=state;case['review_note']=note;case['version']+=1
        else:
            if case['status']!='approved':fail(409,'只有人工已同意的申请可以记录完成')
            if case.get('stale_since_message_id'):fail(409,'顾客已更正或撤回旧申请，请先核对并更新方案')
            note=str(data.get('note','')).strip()
            if not note:fail(422,'请填写处理结果说明')
            # Approval does not reserve a right to ship a part after its entire
            # unit has since been returned/replaced. Recheck inside this write
            # transaction before any inventory or fulfillment mutation.
            _validate_case(c,user,case,case_id)
            if case.get('reship_kind')!='part' and case['type'] in ('return_refund','exchange'):
                prior_parts=[json.loads(row[0]) for row in c.execute("SELECT data FROM cases WHERE order_id=? AND status='completed' AND id!=?",(case['order_id'],case_id))]
                part_ids=[item['id'] for item in prior_parts if item.get('reship_kind')=='part']
                if part_ids:
                    case['accessory_return_check']={'status':'required','part_case_ids':part_ids,'note':'整件退换涉及已补配件，请人工核对是否应随商品退回，不自动额外扣款。'}
                    note+='；本订单曾补发配件，已关联记录供人工核对回收。'
            if data.get('return_handling') in ('returned','scheduled','waived'):
                case['return_handling']=data['return_handling'];case['return_note']=str(data.get('return_note','')).strip()
            if re.search(r'免退|无需退回|不需退回',note) and not case.get('return_handling'):
                case['return_handling']='waived';case['return_note']=note
            issue=case_facts.values(case.get('facts',{})).get('issue_type')
            if case['type']=='reship' and case.get('reship_kind')!='part' and issue!='missing':
                if case.get('return_handling') not in ('returned','scheduled','waived'):
                    fail(422,'整件破损补发前，请记录原件回收安排或明确免退处理')
                if case.get('return_handling')=='waived' and not case.get('return_note'):
                    fail(422,'免退补发需要人工填写免退说明')
            if case['type'] in ('exchange','reship'):
                _move(c,case['part_sku_id'] if case.get('reship_kind')=='part' else case['target_sku_id'],-case['quantity'],'售后演示处理',case_id)
                _record_fulfillment(c,case)
            case['status']='completed';case['completion_note']=note;case['version']+=1
        _case_history(case,action,user,note)
        _store_case(c,case);audit(c,user,'售后:'+action,case_id,before,case)
        state_name={'draft':'方案已更新，请重新确认','submitted':'已提交，等待人工审核','needs_info':'请补充材料','approved':'人工已同意，等待处理','rejected':'人工未同意本次申请','completed':'人工已记录演示处理完成'}[case['status']]
        result=_case(c,user,case_id)
        _add_message(c,case['conversation_id'],'system',f'售后进度：{state_name}。{note}',cards=[{'type':'case','data':result}])
        remember(c,user,f'case:{case_id}:{action}',req,{'id':case_id})
        return result


def submit_case(user,cid,data):
    out=_case_action(user,cid,data,'submit')
    if out.get('_reconfirm'):fail(409,'同订单的其他申请已更新退款分摊，请刷新并重新确认申请金额')
    return out
def supplement_case(user,cid,data):return _case_action(user,cid,data,'supplements')
def review_case(user,cid,data):return _case_action(user,cid,data,'review')
def complete_case(user,cid,data):return _case_action(user,cid,data,'complete')


def admin_data(user):
    require_staff(user)
    with db.connect() as c:
        audits=[]
        for row in c.execute('SELECT * FROM audit ORDER BY rowid DESC LIMIT 100'):
            a=dict(row)
            for k in ['before_data','after_data']:a[k]=json.loads(a[k]) if a[k] else None
            audits.append(a)
        return {'products':_products(c),'policy':_policy(c),'promotions':_promotions(c),'policy_history':[json.loads(r[0]) for r in c.execute('SELECT data FROM policies ORDER BY version DESC')],'audit':audits}


def update_sku(user,sid,data):
    require_staff(user)
    with db.transaction() as c:
        part=c.execute('SELECT * FROM part_skus WHERE id=?',(sid,)).fetchone()
        if part:
            old=dict(part)
            if data.get('expected_version')!=old['version']:fail(409,'配件库存已更新，请刷新')
            stock=positive(data.get('stock',old['stock']),'库存',0,1000000)
            restock=data.get('restock_at',old['restock_at']) or None
            if restock:
                try:datetime.fromisoformat(restock.replace('Z','+00:00'))
                except (ValueError,TypeError,AttributeError):fail(422,'补货时间格式不正确')
            c.execute('UPDATE part_skus SET stock=?,restock_at=?,version=version+1 WHERE id=?',(stock,restock,sid))
            if stock!=old['stock']:c.execute('INSERT INTO inventory_movements VALUES (?,?,?,?,?,?)',(db.uid('stock'),sid,stock-old['stock'],'人工调整配件库存',user['id'],db.now()))
            result=dict(c.execute('SELECT * FROM part_skus WHERE id=?',(sid,)).fetchone())
            audit(c,user,'更新配件库存',sid,old,result)
            return result
        s=_sku(c,sid)
        if data.get('expected_version')!=s['version']:fail(409,'库存资料已更新，请刷新后重试')
        stock=positive(data.get('stock',s['stock']),'库存',0,1000000)
        price=positive(data.get('price_cents',s['price_cents']),'价格（分）',1,100000000)
        restock=data.get('restock_at',s['restock_at']) or None
        if restock:
            try:datetime.fromisoformat(restock.replace('Z','+00:00'))
            except (ValueError,TypeError,AttributeError):fail(422,'补货时间格式不正确')
        c.execute('UPDATE skus SET stock=?,price_cents=?,restock_at=?,version=version+1 WHERE id=?',(stock,price,restock,sid))
        if stock!=s['stock']:c.execute('INSERT INTO inventory_movements VALUES (?,?,?,?,?,?)',(db.uid('stock'),sid,stock-s['stock'],'人工调整库存',user['id'],db.now()))
        out=_sku(c,sid);audit(c,user,'更新库存规格',sid,s,out)
        return out


def update_product(user,pid,data):
    require_staff(user)
    if 'faq' in data:
        fail(422,'标准答案需要先提交审核，请前往标准答案页面')
    with db.transaction() as c:
        old=_product(c,pid);new={k:v for k,v in old.items() if k!='skus'}
        if 'description' in data:
            if not isinstance(data['description'],str) or len(data['description'])>5000:fail(422,'商品描述应为不超过 5000 字的文字')
            new['description']=data['description']
        if 'specs' in data:
            if not isinstance(data['specs'],dict) or len(js(data['specs']))>15000:fail(422,'规格资料格式不正确')
            new['specs']=data['specs']
        if 'faq' in data:
            if not isinstance(data['faq'],list) or len(data['faq'])>30 or any(not isinstance(x,dict) or not isinstance(x.get('q'),str) or not isinstance(x.get('a'),str) for x in data['faq']):fail(422,'常见问答格式不正确')
            new['faq']=data['faq']
        c.execute('UPDATE products SET data=? WHERE id=?',(js(new),pid));audit(c,user,'更新商品资料',pid,old,new)
        return _product(c,pid)


def update_promotions(user,data):
    require_staff(user)
    with db.transaction() as c:
        old=_promotions(c)
        if data.get('expected_version')!=old['version']:fail(409,'活动已更新，请刷新后重试')
        new={**old,**{k:data[k] for k in old if k in data and k!='version'},'version':old['version']+1}
        for k in ['threshold_cents','reduction_cents','special_price_cents']:positive(new[k],k,0,100000000)
        if new['reduction_cents']>new['threshold_cents']:fail(422,'满减金额不能超过门槛金额')
        _product(c,new['special_product_id'])
        try:
            start=datetime.fromisoformat(new['starts_at'].replace('Z','+00:00'));end=datetime.fromisoformat(new['ends_at'].replace('Z','+00:00'))
            if start.tzinfo is None or end.tzinfo is None or start>=end:raise ValueError()
            new['starts_at']=start.astimezone(timezone.utc).isoformat();new['ends_at']=end.astimezone(timezone.utc).isoformat()
        except (ValueError,TypeError,AttributeError):fail(422,'请设置有效的活动起止时间（含时区）')
        if not isinstance(new['enabled'],bool):fail(422,'活动启用状态不正确')
        c.execute('UPDATE promotions SET data=? WHERE id=1',(js(new),));audit(c,user,'发布优惠活动','promotions',old,new)
        return new


def publish_policy(user,data):
    require_staff(user)
    with db.transaction() as c:
        old=_policy(c)
        if data.get('expected_version')!=old['version']:fail(409,'规则已更新，请刷新后重试')
        new={**old,**{k:data[k] for k in ['return_days','exchange_days','quality_days','reship_days','warranty_days','no_reason_shipping','quality_note','extra_note'] if k in data},'version':old['version']+1,'published_at':db.now()}
        positive(new['return_days'],'无理由退货天数',7,365);positive(new['exchange_days'],'换货天数',7,365)
        for field,minimum in [('quality_days',30),('reship_days',30),('warranty_days',365)]:positive(new.get(field,minimum),field,minimum,3650)
        if new['no_reason_shipping'] not in ('customer','store'):fail(422,'请选择有效的运费承担方')
        for key in ['quality_note','extra_note']:
            if not isinstance(new[key],str) or len(new[key])>5000:fail(422,'规则说明应为不超过 5000 字的文字')
        c.execute('INSERT INTO policies VALUES (?,?)',(new['version'],js(new)));audit(c,user,'发布售后规则',str(new['version']),old,new)
        refresh_legacy_assessments(c)
    return new

# Event journals contain public progress/text only; durable state remains in SQLite.
_events = {}


def _event(tid,event):
    events=_events.setdefault(tid,[])
    events.append(event)
    if len(_events)>1000:
        for old in list(_events)[:200]:
            if old!=tid:_events.pop(old,None)


def events_since(tid,index):
    events=_events.get(tid,[])
    return events[index:],len(events)


def _current(c,tid):
    r=c.execute('SELECT turns.*,conversations.mode,conversations.generation AS current_generation FROM turns JOIN conversations ON conversations.id=turns.conversation_id WHERE turns.id=?',(tid,)).fetchone()
    return r if r and r['status']=='pending' and r['mode']=='ai' and r['generation']==r['current_generation'] else None


def agent_is_current(turn_id):
    with db.connect() as c:return bool(_current(c,turn_id))


def agent_usage_snapshot(turn_id):
    """Cancellation invalidates output, not the audit of work already attempted."""
    from . import model_usage
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM turns WHERE id=?',(turn_id,)).fetchone():return
    summary=model_usage.summary(turn_id=turn_id)
    if summary['attempts']:
        with db.transaction() as c:
            c.execute('UPDATE turns SET usage=? WHERE id=?',(js({**summary,**summary.get('known_usage',{})}),turn_id))


def agent_register_manual_requests(turn_id):
    """Bind explicit source instructions to each typed remedy and owned order."""
    from . import support, conversation_state

    def authorization_text(value):
        return re.sub(r'不(?:是)?只(?:是|想)?(?:咨询|问|了解)', '', value)

    def blocked(value):
        value = authorization_text(value)
        return bool(re.search(r'先别|(?:不要|不用|不需要|不想|别|不)(?:再)?(?:办理|处理|登记|申请|转|找)|暂不|只(?:想)?(?:咨询|问|了解)|先(?:咨询|了解)|(?:查|问|了解|咨询).{0,8}资格|如果|假如|假设|要是', value))

    def explicit(value):
        # A polite suffix ("请帮我登记维修可以吗") does not undo an
        # otherwise explicit action. Qualification questions have no action.
        return bool(re.search(r'(?:帮我|替我|麻烦)(?:直接|现在|正式|先|马上)?(?:办理|处理|登记|报修|申请|安排|维修|调查)|请(?:你|人工|人工客服|让人工|帮我)?(?:办理|处理|登记|受理|申请|安排)|我要(?:办理|申请)|(?:转|找)(?:人工|真人)(?:客服)?', authorization_text(value)))

    with db.connect() as c:
        t = _current(c, turn_id)
        if not t:
            return []
        user = json.loads(one(c, 'SELECT data FROM users WHERE id=?', (t['user_id'],))[0])
        conv = _conversation(c, user, t['conversation_id'])
        messages = c.execute("SELECT * FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid DESC LIMIT 2", (conv['id'],)).fetchall()
        if not messages:
            return []
        source = messages[0]
        text = source['content']
        if not explicit(text) or re.search(r'如果|假如|假设|要是', text):
            return []
        orders = [{'id': row[0], 'product_id': json.loads(row[1]).get('product_id')}
                  for row in c.execute('SELECT id,data FROM orders WHERE user_id=?', (user['id'],))]
        named_ids = re.findall(r'(?<![a-z0-9_-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*-\d[a-z0-9_-]*(?![a-z0-9_-])', text, re.I)
        known_ids = {order['id'].lower() for order in orders}
        if conversation_state._has_unknown_order(text, orders) or any(identity.lower() not in known_ids for identity in named_ids):
            return []
        # An unscoped "先别登记" applies to this message, including a prior
        # action clause. A scoped consultation can coexist with another order's
        # explicit instruction without authorizing the consultation itself.
        for clause in re.split(r'[，,。；;！？?\n]', text):
            if (blocked(clause) and not conversation_state.request_types(clause)
                    and not conversation_state._order_mentions(clause, orders)):
                return []
        current_intents = [item for item in conversation_state.intents(c, conv['id'])
                           if item.get('source_message_id') == source['id']
                           and item.get('kind') == 'after_sale' and item.get('status') != 'withdrawn'
                           and item.get('request_type') in ('repair', 'logistics', 'refund_only')]
        # Several orders paired with several remedies do not establish which
        # order gets which service. Preserve the raw message for clarification.
        all_types = {item['request_type'] for item in current_intents}
        if len(all_types) > 1 and any(len(item.get('order_ids', [])) > 1 for item in current_intents):
            return []
        global_authorization = explicit(text) and not blocked(text)
        owned = {order['id'] for order in orders}
        mentioned = conversation_state._order_mentions(text, orders)
        bindings = {}
        for intent in current_intents:
            clause = intent.get('text', '')
            clause_authorized = explicit(clause)
            if blocked(clause) or not (clause_authorized or global_authorization):
                continue
            # Authorization for A must not convert B's eligibility question
            # into a submitted request. An explicit action with a polite
            # "可以吗" remains authorized by its own clause.
            if not clause_authorized and re.search(r'能不能|能否|是否|可否|可以|怎么|如何|吗|资格|条件|流程|能(?:维修|修理|返修|退款|仅退款|只退款|办理)', clause):
                continue
            order_ids = list(dict.fromkeys(intent.get('order_ids', [])))
            # A short "这单/继续" may reference one immediately preceding
            # order of the same remedy; never scan older topics or guess across
            # multiple orders, an explicit unknown order, or another product.
            if (not order_ids and not mentioned and len(current_intents) == 1
                    and len(messages) > 1 and re.search(r'这单|这笔|刚才|继续', text)
                    and not intent.get('product_ids')):
                previous = [i for i in conversation_state.intents(c, conv['id'])
                            if messages[1]['id'] in i.get('source_message_ids', [])
                            and i.get('kind') == 'after_sale'
                            and i.get('request_type') == intent['request_type']
                            and i.get('status') != 'withdrawn']
                prior_orders = {oid for i in previous for oid in i.get('order_ids', [])}
                if len(prior_orders) == 1:
                    order_ids = list(prior_orders)
            if not order_ids:
                # Explicit intake may precede order identification. Keep it
                # unassigned instead of inventing a link to a selected order.
                if mentioned or intent.get('product_ids') or re.search(r'这单|这笔|刚才|继续',text):
                    continue
                bindings[(None, intent['request_type'])] = intent
                continue
            if not set(order_ids).issubset(owned):continue
            for order_id in order_ids:
                bindings[(order_id, intent['request_type'])] = intent
    result = []
    for (order_id, kind), intent in bindings.items():
        item = support.create_request(user, {'conversation_id': conv['id'], 'order_id': order_id, 'kind': kind,
            'reason': text, 'explicit_request': True, 'source_message_id': source['id'], 'intent_id': intent['id'],
            'request_id': f'intake:{source["id"]}:{order_id or "unassigned"}:{kind}'}, guard_turn=turn_id)
        result.append(item)
    return result


def agent_context(turn_id):
    from . import conversation_state
    with db.transaction() as c:
        t=_current(c,turn_id)
        if not t:fail(409,'当前回复已失效')
        user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(t['user_id'],))[0]);conv=_conversation(c,user,t['conversation_id'])
        messages=[_message(r) for r in c.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid',(conv['id'],))]
        for m in messages:m['attachment_ids']=m['attachments']
        resumed=c.execute("SELECT 1 FROM turn_origins WHERE turn_id=? AND kind='resume'",(turn_id,)).fetchone()
        requests=[json.loads(r[0]) for r in c.execute('SELECT data FROM service_requests WHERE user_id=? AND conversation_id=?',(user['id'],conv['id']))]
        return {'turn_id':turn_id,'conversation_id':conv['id'],'user_id':user['id'],'generation':conv['generation'],'resume_origin':bool(resumed),'preferences':conversation_state.safe_sync(c,conv,_products(c)),'intents':conversation_state.intents(c,conv['id']),'messages':messages,'products':_products(c),'orders':_orders(c,user),'cases':[_case(c,user,r[0]) for r in c.execute('SELECT id FROM cases WHERE user_id=?',(user['id'],))],'service_requests':requests,'policy':_policy(c),'selected_product_id':conv['selected_product_id'],'selected_order_id':conv['selected_order_id']}


def agent_images(turn_id):
    with db.connect() as c:
        t=_current(c,turn_id)
        if not t:return []
        recent=c.execute("SELECT content,attachments FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid DESC LIMIT 8",(t['conversation_id'],)).fetchall()
        m=recent[0] if recent else None
        if not m:return []
        attachments=json.loads(m['attachments'])
        # Explicit follow-ups about a recent photo must re-examine the original,
        # not treat a prior model description as visual evidence. Keep ownership
        # checks below and never cross the current conversation or old window.
        if not attachments and re.search(r'图片|照片|这张图|那张图|正面图|看图|图里|图中|图上|看不清|(?:读到|看到|识别到|识别出)(?:型号|序列号|标签)',m['content'] or ''):
            attachments=next((json.loads(row['attachments']) for row in recent[1:] if json.loads(row['attachments'])),[])
        result=[]
        for aid in attachments:
            row=c.execute('SELECT * FROM attachments WHERE id=? AND user_id=?',(aid,t['user_id'])).fetchone()
            if row:result.append({'id':row['id'],'path':row['path'],'mime_type':row['mime_type']})
        return result


def agent_status(turn_id,phase):
    with db.transaction() as c:
        if not _current(c,turn_id):return False
        c.execute('UPDATE turns SET phase=?,updated_at=? WHERE id=?',(phase,db.now(),turn_id))
    _event(turn_id,{'type':'status','phase':phase})
    return True


def agent_delta(turn_id,text):
    if not agent_is_current(turn_id):return False
    _event(turn_id,{'type':'delta','text':text,'content':text})
    return True


def _grounded_draft_status_prose(content, drafts, active_case_ids=()):
    """Keep final chat instructions aligned with saved, current-conversation drafts.

    The writer may have an assessment without a draft, or a draft whose rule
    decision still needs facts. Neither is a ready-to-submit application.
    Only narrow affirmative claims are rewritten; independent business facts
    and explicit hypothetical instructions are left intact.
    """
    if not isinstance(content, str) or not content:
        return content
    if not drafts:
        def replace_claim(pattern, replacement, value):
            def guarded(match):
                before = value[max(0, match.start() - 32):match.start()]
                prefix = re.split(r'[，,。！？；;\n]', before)[-1]
                sentence_prefix = re.split(r'[。！？；;\n]', before)[-1]
                if (re.search(r'(?:没有|尚无|并未|不能说|不代表|如果|假如|假设).{0,8}$', prefix)
                        or re.search(r'(?:待|保存后|创建后|整理后).{0,20}$', sentence_prefix)):
                    return match.group()
                return replacement
            return re.sub(pattern, guarded, value)
        content = replace_claim(r'(?:目前|现在)?(?:只是|仅是)?待确认的申请草稿',
                                '目前尚无已保存的申请草稿', content)
        content = replace_claim(r'申请已(?:经)?整理好|(?:已经|已)(?:为你|帮你)?(?:创建|生成|保存)(?:好|了)?(?:一份)?申请草稿',
                                '目前尚无已保存的申请草稿', content)
        content = replace_claim(r'补发申请(?:目前|现在)?(?:还|仍)?是?待审核草稿|申请(?:目前|现在)?(?:还|仍)?是?待审核草稿',
                                '目前尚无已保存的补发申请草稿', content)
        content = replace_claim(r'请核对确认后提交申请|请打开申请卡[^。；;\n]{0,65}?(?:确认提交申请|提交申请)',
                                '如需办理，请先明确要求整理申请草稿；保存后由你在申请卡核对并确认提交', content)
        return content
    if any(case.get('id') in active_case_ids
           and (case.get('assessment') or {}).get('decision') == 'needs_info'
           for case in drafts):
        content = re.sub(r'申请已(?:经)?整理好，请核对(?:下方信息|申请详情)?后确认提交',
                         '已有待补事实的申请草稿；请先补齐上述事实并重新核对', content)
        content = re.sub(r'核对申请后提交', '补齐事实并重新核对申请', content)
    return content


def _publication_unit_quotes(c, user, turn, snapshot):
    """Verify private, observed quote IDs without adding offers or audit rows."""
    if not isinstance(snapshot, dict) or snapshot.get('source') != 'fresh_database_and_current_tools':
        return {}
    skus = {s['id']: s for p in snapshot.get('products', []) if isinstance(p, dict)
            for s in p.get('skus', []) if isinstance(s, dict) and s.get('id')}
    result, stamp = {}, db.now()
    for qid in snapshot.get('verification_quote_ids', []):
        if not isinstance(qid, str) or qid in result:
            continue
        row = c.execute('SELECT data FROM quotes WHERE id=? AND user_id=? AND created_at>=? AND created_at<=?',
                        (qid, user['id'], turn['created_at'], stamp)).fetchone()
        if not row:
            continue
        quote = json.loads(row[0])
        sku = skus.get(quote.get('sku_id'))
        if (not sku or quote.get('quantity') != 1 or quote.get('quote_id') != qid
                or quote.get('unit_price_cents') != sku.get('price_cents')
                or quote.get('sku_version') != sku.get('version')):
            continue
        try:
            current = _quote(c, user, quote['sku_id'], 1, persist=False)
        except HTTPException:
            continue
        # Rechecking money also rejects a promotion that expired during the
        # reply without a version change. Availability stays with the later
        # catalogue projection, not with this quote's earlier stock value.
        keys = ('unit_price_cents', 'subtotal_cents', 'discount_cents', 'total_cents',
                'sku_version', 'promotion_version')
        if all(quote.get(key) == current.get(key) for key in keys):
            result[qid] = quote
    return result


def _grounded_application_status_prose(c, turn, content):
    """Ground affirmative application/work-ticket states at the write boundary.

    Assessments and model cards are not mutation receipts. Re-read actual rows
    in this transaction, retaining owner, order, remedy and conversation scope.
    A customer's explicit historical lookup may name a real older record;
    that record cannot substantiate a new application in this conversation.
    """
    if not isinstance(content, str) or not content:
        return content
    uid, cid = turn['user_id'], turn['conversation_id']
    customer_texts = [row[0] for row in c.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid", (cid,))]
    question = customer_texts[-1] if customer_texts else ''
    order_ids = [row[0] for row in c.execute('SELECT id FROM orders')]
    canonical_orders = {identity.lower(): identity for identity in order_ids}
    order_pattern = '|'.join(re.escape(identity) for identity in sorted(order_ids, key=len, reverse=True))
    order_pattern = (r'(?<![a-z0-9_-])(?:' + order_pattern + r'|(?:DEMO|EVAL|ORDER)[a-z0-9_-]+)(?![a-z0-9_-])'
                     if order_pattern else r'(?<![a-z0-9_-])(?:DEMO|EVAL|ORDER)[a-z0-9_-]+(?![a-z0-9_-])')

    def orders_in(value):
        found = [(match.start(), canonical_orders.get(match.group().lower(), match.group()))
                 for match in re.finditer(order_pattern, value, re.I)]
        found.extend((match.start(1), canonical_orders.get(match[1].lower(), match[1]))
                     for match in re.finditer(r'订单(?:号|编号)?\s*[:：]?\s*([a-z0-9][a-z0-9_-]{2,})', value, re.I))
        return sorted(set(found))

    records = []
    for table in ('cases', 'service_requests'):
        for row in c.execute(f'''SELECT r.* FROM {table} r LEFT JOIN orders o ON o.id=r.order_id
                                WHERE r.user_id=? AND (r.order_id IS NULL OR o.user_id=?)''', (uid, uid)):
            data = json.loads(row['data'])
            records.append({'id': row['id'], 'order_id': row['order_id'], 'status': row['status'],
                            'conversation_id': row['conversation_id'], 'table': table,
                            'kind': data.get('type') if table == 'cases' else row['kind'],
                            'approved': row['status'] == 'approved' or (row['status'] == 'completed' and
                                (table == 'cases' or any(h.get('action') == 'approve' for h in data.get('history', []))))})
    referenced_ids = set(re.findall(r'(?<![a-z0-9_])(?:case|service)_[a-z0-9_]+', question, re.I))
    history_query = bool(re.search(r'(?:状态|进度|已有.{0,6}(?:申请|工单|记录)|现有.{0,6}(?:申请|工单|记录)|'
                                   r'(?:查|查询|查看|核对|列出).{0,12}(?:申请|工单|售后记录|受理记录))', question))
    question_orders = {identity for _, identity in orders_in(question)}
    selected = one(c, 'SELECT selected_order_id FROM conversations WHERE id=?', (cid,))[0]
    available = [r for r in records if r['conversation_id'] == cid or r['id'] in referenced_ids or
                 (history_query and (not question_orders or r['order_id'] in question_orders)
                  and (question_orders or not selected or r['order_id'] == selected))]
    fallback = next(iter(question_orders)) if len(question_orders) == 1 else selected if not question_orders else None
    referenced_orders = {record['order_id'] for record in records if record['id'] in referenced_ids}
    if not question_orders and len(referenced_orders) == 1:
        fallback = next(iter(referenced_orders))
    if not fallback:
        scope_orders = {r['order_id'] for r in available}
        if not question_orders and len(scope_orders) == 1:
            fallback = next(iter(scope_orders))

    def remedies(value):
        return {kind for kind, pattern in (
            ('refund_only', r'仅退款|只退款|只退钱'), ('return_refund', r'退货|退回'),
            ('exchange', r'换货|换成'), ('reship', r'补发|补寄|欠发|漏.{0,5}(?:线|配件)'),
            ('repair', r'维修'), ('logistics', r'物流核实|核实物流'), ('followup', r'未解决跟进'))
            if re.search(pattern, value)}

    def compact(value):
        return re.sub(r'[\s。！？!?，,；;“”‘’「」\"]', '', value)
    quotes = [(match.start(), match.end()) for match in re.finditer(r'“[^”]*”|‘[^’]*’|「[^」]*」|"[^"\n]*"', content)
              if compact(match.group()[1:-1]) and any(compact(match.group()[1:-1]) in compact(text) for text in customer_texts)]
    patterns = {
        'completed': r'(?:已经|已)(?:为你|帮你)?(?:处理|办理)(?:完成|完毕|好了|好|了)?|(?:处理|办理)(?:已经|已)?(?:完成|完毕)|(?:已经|已)完成',
        'approved': r'(?:人工|客服|售后)?(?:已经|已)(?:审核通过|通过(?:人工)?审核|获(?:得)?(?:人工)?(?:批准|同意)|(?:由)?人工(?:批准|同意)|批准|同意)',
        'rejected': r'(?:人工|客服|售后)?(?:已经|已)(?:拒绝|驳回)',
        'processing': r'(?:人工|客服|售后)?(?:已经|已)(?:接单|受理|开始处理|开始办理)|(?:正在|已在)(?:处理|办理|审核)',
        'registered': r'(?:已经|已)(?:为你|帮你|替你)?登记(?:了|成功|完成)?|登记(?:成功|完成)',
        'submitted': r'(?:已经|已)(?:为你|帮你|替你)?(?:提交|递交)(?:了|成功|完成)?|提交(?:成功|完成)',
        'review': r'(?:等待|待)(?:人工|客服|售后)?审核|审核中',
    }
    statuses = re.compile('|'.join(f'(?P<{name}>{pattern})' for name, pattern in patterns.items()))
    owned_orders = {row[0] for row in c.execute('SELECT id FROM orders WHERE user_id=?', (uid,))}

    def actual_state(candidates, order, ids, kind):
        states = {record['status'] for record in candidates}
        labels = {
            'draft': '当前对应申请仍为待顾客确认的草稿，尚未提交；请先核对草稿，由顾客确认提交后再交人工审核',
            'submitted': '当前对应记录已提交，仍待人工审核；尚未记录人工同意或处理完成',
            'processing': '当前对应工单已由人工接单，正在核实；尚未记录处理完成',
            'needs_info': '当前对应记录待补充材料；请通过原记录补交后再由人工核实',
            'awaiting_confirmation': '当前方案待顾客确认；须由顾客确认当前版本后再由人工终审',
            'approved': '当前对应申请人工已同意，仍待处理；尚未记录处理完成',
            'rejected': '当前对应记录已被人工拒绝；请查看实际审核说明',
            'completed': '当前对应记录已由人工记录本地处理完成；请查看实际处理说明',
        }
        if len(states) == 1 and next(iter(states)) in labels:
            return labels[next(iter(states))]
        if states or ids or order not in owned_orders:
            return '当前尚未唯一核实该申请或工单状态；请明确本人申请编号或订单后核对实际记录'
        if kind in ('repair', 'logistics', 'refund_only', 'followup'):
            return '本会话尚无对应的人工受理记录；如需办理，请明确办理诉求后登记受理，由人工核实'
        return '本会话尚无对应的已提交申请；如需办理，请先整理并核对申请草稿，由顾客确认提交后再交人工审核'

    def supports(record, status):
        return (
            (status in ('submitted', 'registered') and record['status'] in
                ('submitted', 'processing', 'needs_info', 'awaiting_confirmation', 'approved', 'rejected', 'completed'))
            or (status == 'review' and (record['status'] == 'submitted' or
                (record['table'] == 'service_requests' and record['status'] == 'processing')))
            or (status == 'approved' and record['approved'])
            or (status == 'rejected' and record['status'] == 'rejected')
            or (status == 'processing' and record['table'] == 'service_requests' and record['status'] == 'processing')
            or (status == 'completed' and record['status'] == 'completed'))
    current_order = fallback
    current_kinds = remedies(question)
    kind_names = {'return_refund': '退货申请', 'exchange': '换货申请', 'reship': '补发申请',
                  'repair': '维修受理', 'logistics': '物流核实受理', 'refund_only': '仅退款受理', 'followup': '跟进工单'}
    replacements = {}
    for sentence in re.finditer(r'[^。！？!?；;\n]+', content):
        value = sentence.group()
        order_refs = orders_in(value)
        prior_order = current_order
        if order_refs:
            current_order = order_refs[-1][1]
        if not re.search(r'申请|工单|受理|售后|维修|补发|补寄|跟进', value):
            continue
        if not statuses.search(value):
            current_kinds = remedies(value) or current_kinds
            continue
        current_scope = None
        for match in statuses.finditer(value):
            start, end = sentence.start() + match.start(), sentence.start() + match.end()
            before, after = value[:match.start()], value[match.end():]
            clause_before = re.split(r'[，,]', before)[-1]
            if any(left <= start and end <= right for left, right in quotes):
                continue
            if (re.search(r'(?:没有|尚未|并未|未曾|并非|不是|不代表|不意味|不等于|不能(?:说|称)?|'
                          r'无法(?:说|称)?|不要(?:说|称)).{0,14}$', clause_before)
                    or re.search(r'如果|假如|假设|若|一旦|将来|未来|(?:提交|确认|保存|登记)后', before)
                    or re.match(r'(?:后|时|之后|才能)', after)):
                continue
            if (re.search(r'你(?:刚才)?(?:说|提到|表示)|顾客(?:原话|说|表示)', clause_before)
                    and any(compact(match.group()) in compact(text) for text in customer_texts)):
                continue
            # Definitions and advice describe a possible state, not this record.
            if (re.match(r'的.{0,12}(?:建议|方案|评估)', after)
                    or (re.match(r'的.{0,12}(?:申请|工单)', after)
                        and re.search(r'不能|不得|需|须|仍可|可以', after)
                        and not re.search(r'你|我|这份|该份|当前|本次|本轮', before))):
                continue
            preceding = [identity for position, identity in order_refs if position < match.start()]
            order = preceding[-1] if preceding else order_refs[0][1] if len(order_refs) == 1 else prior_order
            separators = list(re.finditer(r'[，,]|而且|另外|同时|以及|并且', value))
            left = max((part.end() for part in separators if part.end() <= match.start()), default=0)
            right = min((part.start() for part in separators if part.start() >= match.end()), default=len(value))
            clause = value[left:right]
            clause_kinds = remedies(clause)
            current_kinds = clause_kinds or current_kinds
            ids = set(re.findall(r'(?<![a-z0-9_])(?:case|service)_[a-z0-9_]+', clause, re.I))
            clause_orders = orders_in(clause)
            if not clause_orders and not clause_kinds and not ids and current_scope:
                required = current_scope  # "等待审核" keeps the preceding claim's full subject.
            else:
                required = set()
                if clause_orders:
                    for index, (position, identity) in enumerate(clause_orders):
                        stop = clause_orders[index + 1][0] if index + 1 < len(clause_orders) else len(clause)
                        kinds = remedies(clause[position:stop]) or current_kinds
                        required.update((identity, kind) for kind in kinds or {None})
                elif ids:
                    # Explicit record IDs identify their actual order; a UI
                    # selection for another order cannot override that identity.
                    for record in available:
                        if record['id'] in ids:
                            required.update((record['order_id'], kind) for kind in clause_kinds or {record['kind']})
                    if not required:
                        required.update((order, kind) for kind in current_kinds or {None})
                else:
                    subject_orders = {identity for identity, _ in current_scope} if current_scope else {order}
                    required.update((identity, kind) for identity in subject_orders for kind in current_kinds or {None})
            current_scope = required
            candidates = [r for r in available if not ids or r['id'] in ids]
            if re.search(r'本次|本轮|这次|新(?:的)?(?:申请|工单)|刚(?:刚)?(?:为你|帮你)', before):
                candidates = [r for r in candidates if r['conversation_id'] == cid]
            status = match.lastgroup
            scoped = [(identity, kind, [r for r in candidates if r['order_id'] == identity
                        and (kind is None or r['kind'] == kind)]) for identity, kind in required]
            # Cover each asserted order/remedy pair, not just the final order or
            # any one matching row. Separate clauses retain separate bindings.
            covered_ids = {r['id'] for _, _, rows in scoped for r in rows}
            valid = bool(scoped) and ids <= covered_ids and all(
                rows and all(supports(record, status) for record in rows) for _, _, rows in scoped)
            if not valid:
                # Replace the status clause, not just 已 -> 尚未: otherwise a
                # writer's "按尚未提交的申请审核" remains a false instruction.
                if re.fullmatch(r'[，,]\s*这里不重复', value[right:]):
                    right = len(value)
                multiple_orders = len({identity for identity, _, _ in scoped}) > 1
                replacement = '；'.join(
                    (f'订单{identity}·' if multiple_orders else '') + kind_names.get(kind, '申请或工单') + '：'
                    + actual_state(rows, identity, ids, kind)
                    for identity, kind, rows in sorted(scoped, key=lambda item: (item[0] or '', item[1] or '')))
                if ids - covered_ids:
                    replacement = '当前尚未唯一核实该申请或工单状态；请明确本人申请编号或订单后核对实际记录'
                replacements[(sentence.start() + left, sentence.start() + right)] = replacement
    for (start, end), replacement in sorted(replacements.items(), reverse=True):
        content = content[:start] + replacement + content[end:]
    return content


def agent_finish(turn_id,content,cards,usage=None,elapsed_ms=0,intent_outcomes=None,publication_snapshot=None):
    with db.transaction() as c:
        t=_current(c,turn_id)
        if not t:return False
        from . import card_constraints,conversation_state,model_usage
        if any(card.get('type') in ('product','quote') for card in cards):
            user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(t['user_id'],))[0])
            conv=_conversation(c,user,t['conversation_id']);catalogue=_products(c)
            preferences=conversation_state.safe_sync(c,conv,catalogue)
            # Internal server-authored publication data only. It is not a model
            # card or a client input: the agent constructs this from this turn's
            # fresh catalogue and ordered service tool observations. Preserve
            # that query-time view for both prose and cards; checkout still
            # checks live inventory and the stored quote separately.
            if isinstance(publication_snapshot,dict) and publication_snapshot.get('source')=='fresh_database_and_current_tools':
                projected={p['id']:p for p in publication_snapshot.get('products',[]) if isinstance(p,dict) and p.get('id')}
                catalogue=[projected.get(p['id'],p) for p in catalogue]
            messages=[_message(r) for r in c.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid',(conv['id'],))]
            quote_ids={card.get('data',{}).get('quote_id') for card in cards if card.get('type')=='quote'}
            quotes={}
            for qid in quote_ids:
                row=c.execute('SELECT data FROM quotes WHERE id=? AND user_id=?',(qid,t['user_id'])).fetchone()
                if row:quotes[qid]=json.loads(row[0])
            quotes.update(_publication_unit_quotes(c, user, t, publication_snapshot))
            edits={i.get('source_message_id') for i in preferences.get('items',[]) if str(i.get('source_message_id','')).startswith('customer_edit:')}
            edits.update(source.get('source_message_id') for record in preferences.get('conditional_requests', [])
                         for source in record.get('constraint_sources', {}).values()
                         if str(source.get('source_message_id', '')).startswith('customer_edit:'))
            cards=card_constraints.annotate(cards,{'products':catalogue,'preferences':preferences,'messages':messages,
                'orders':_orders(c,user),'selected_product_id':conv.get('selected_product_id'),'customer_edit_sources':edits,
                'catalog_evidence':{'source':'fresh_database','turn_id':turn_id}},quotes)
        user = json.loads(one(c, 'SELECT data FROM users WHERE id=?', (t['user_id'],))[0])
        drafts = [_case(c, user, row[0]) for row in c.execute(
            "SELECT id FROM cases WHERE user_id=? AND conversation_id=? AND status='draft'",
            (t['user_id'], t['conversation_id']))]
        active_case_ids = {card.get('data', {}).get('id') for card in cards
                           if card.get('type') == 'case' and isinstance(card.get('data'), dict)}
        content = _grounded_draft_status_prose(content, drafts, active_case_ids)
        content = _grounded_application_status_prose(c, t, content)
        message=_add_message(c,t['conversation_id'],'assistant',content,cards=cards,turn_id=turn_id)
        persisted=model_usage.summary(turn_id=turn_id)
        if persisted.get('attempts'):usage={**persisted,**persisted.get('known_usage',{})}
        latest=c.execute("SELECT id FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid DESC LIMIT 1",(t['conversation_id'],)).fetchone()
        if latest and intent_outcomes:
            conversation_state.record_outcomes(c,t['conversation_id'],latest[0],[{**item,'answer_message_id':message['id']} for item in intent_outcomes])
        c.execute("UPDATE turns SET status='completed',phase='回复完成',answer=?,usage=?,elapsed_ms=?,updated_at=? WHERE id=?",(content,js(usage),elapsed_ms,db.now(),turn_id))
    _event(turn_id,{'type':'done','answer':content})
    return True


def agent_fail(turn_id,error):
    with db.transaction() as c:
        t=_current(c,turn_id)
        if not t:return False
        from . import model_usage
        usage=model_usage.summary(turn_id=turn_id)
        # A provider failure after a successful tool mutation must not hide the
        # persisted result. Re-read only this customer's conversation records;
        # never replay a mutation or turn a failed response into a completed one.
        saved = []
        user = json.loads(one(c, 'SELECT data FROM users WHERE id=?', (t['user_id'],))[0])
        for row in c.execute('SELECT data FROM cases WHERE user_id=? AND conversation_id=?', (t['user_id'], t['conversation_id'])):
            case = json.loads(row[0])
            if any(h.get('turn_id') == turn_id and h.get('action') in ('draft', 'amend', 'reuse') for h in case.get('history', [])):
                saved.append(_case(c, user, case['id']))
        content = ''
        if saved:
            labels = {'draft':'待顾客确认', 'submitted':'待人工审核', 'needs_info':'待补充材料', 'approved':'已批准、待记录执行',
                      'completed':'本地处理记录已完成', 'rejected':'已拒绝', 'cancelled':'已取消'}
            content = error + '\n\n已重新核对本轮涉及的实际申请：\n' + '\n'.join(
                f"{item['id']}（版本 {item['version']}）：{labels.get(item['status'], item['status'])}。"
                for item in saved) + '\n请查看下方申请记录。上述仅说明实际保存状态，本次完整回复仍未完成。'
            _add_message(c, t['conversation_id'], 'assistant', content,
                         cards=[{'type':'case', 'data':item} for item in saved], turn_id=turn_id, status='failed')
        c.execute("UPDATE turns SET status='failed',phase='暂时未能完成回复',answer=?,error=?,usage=?,updated_at=? WHERE id=?",(content,error,js(usage),db.now(),turn_id))
    _event(turn_id,{'type':'error','error':error})
    return True


def _same_agent_draft_payload(case, args):
    """A retry may reuse an unchanged draft; a correction always needs a revision."""
    if case.get('status') != 'draft' or case.get('stale_since_message_id'):
        return False
    keys = ('type', 'quantity', 'reason', 'condition_note', 'target_sku_id',
            'reship_kind', 'part_sku_id')
    if any(key in args and args[key] != case.get(key) for key in keys):
        return False
    if 'facts' in args:
        original = {key: value for key, value in (case.get('facts') or {}).items()
                    if value.get('source') != 'order_inference'}
        try:
            if case_facts.normalize(args['facts']) != original:
                return False
        except (TypeError, ValueError):
            return False
    return any(key in args for key in ('reason', 'condition_note', 'facts'))


def _reuse_agent_draft(user, case_id, version, args, turn_id, request_id):
    with db.transaction() as c:
        t = _current(c, turn_id)
        if not t:
            fail(409, '回复已失效，不能沿用旧申请')
        case = _case(c, user, case_id)
        if (case['version'] != version or case['conversation_id'] != t['conversation_id']
                or case['order_id'] != args.get('order_id') or case['type'] != args.get('type')
                or not _same_agent_draft_payload(case, args)):
            return None
        if case['updated_at'] > t['created_at'] and (case.get('history') or [{}])[-1].get('turn_id') != turn_id:
            fail(409, '申请在本次回复期间已更新，旧回复不能沿用新草稿')
        _case_history(case, 'reuse', user, '原草稿内容未变，本轮沿用原版本供顾客核对', turn_id)
        _store_case(c, case)
        remember(c, user, 'agentdraft', request_id, {'id': case_id})
        return _case(c, user, case_id)


def agent_tool(turn_id,name,args):
    allowed={'search_products','get_product','get_inventory','quote_price','get_orders','get_order','get_cases','get_policy','get_parts','evaluate_after_sale','draft_case','request_human'}
    if name not in allowed:return {'error':'这个工具不在客服权限范围内'}
    if not isinstance(args,dict):return {'error':'工具参数格式不正确'}
    if any(k in args and not isinstance(args[k],str) for k in ['product_id','sku_id','order_id','target_sku_id']):return {'error':'商品或订单标识格式不正确'}
    with db.connect() as c:
        t=_current(c,turn_id)
        if not t:return {'error':'会话已转交人工或回复已取消'}
        user=json.loads(one(c,'SELECT data FROM users WHERE id=?',(t['user_id'],))[0]);cid=t['conversation_id']
    try:
        if name=='search_products':return products(str(args.get('query','')),str(args.get('category','')))
        if name=='get_product':return product(args.get('product_id'))
        if name=='get_inventory':return inventory(args.get('sku_id'))
        if name=='quote_price':return quote(user,args)
        if name=='get_orders':return orders(user)
        if name=='get_order':return order(user,args.get('order_id'))
        if name=='get_cases':return get_cases(user,args.get('order_id'))
        if name=='get_policy':return policy()
        if name=='get_parts':return get_parts(user,args.get('order_id'))
        if name in ('evaluate_after_sale', 'draft_case'):
            # Re-read customer text at the write boundary. Model-supplied
            # flags cannot authorize either creation or an implicit amendment.
            from . import agent
            context = agent_context(turn_id)
            if agent.assessment_only_request(context, args.get('order_id'), args.get('type')):
                args = {**args, '_read_only_draft': True}
                name = 'evaluate_after_sale'
        draft_target=(args.get('_draft_target_id'),args.get('_draft_target_version')) if '_draft_target_id' in args else None
        if name=='evaluate_after_sale':return evaluate_after_sale(user,args,guard_turn=turn_id,update_draft=not args.get('_read_only_draft',False),draft_target=draft_target)
        if name=='draft_case':
            if draft_target is not None:
                # An amendment is never allowed to fall through to creation or
                # to whichever draft happens to be the latest at execution time.
                result=evaluate_after_sale(user,args,guard_turn=turn_id,update_draft=not args.get('_read_only_draft',False),draft_target=draft_target)
                return result['case']
            req=f"agent:{turn_id}:{args.get('order_id')}:{args.get('type')}"
            with db.connect() as c:
                cached=memo(c,user,'case:create',req) or memo(c,user,'agentdraft',req)
                if cached:return _case(c,user,cached['id'])
                drafts=c.execute("SELECT id,version FROM cases WHERE conversation_id=? AND order_id=? AND user_id=? AND status='draft' ORDER BY rowid DESC",(cid,args.get('order_id'),user['id'])).fetchall()
                old=next((row for row in drafts if _case(c,user,row['id'])['type']==args.get('type')),None)
            if old:
                reused=_reuse_agent_draft(user,old['id'],old['version'],args,turn_id,req)
                if reused:return reused
                return patch_case(user,old['id'],{**args,'expected_version':old['version']},guard_turn=turn_id,agent_request=req)
            return create_case(user,{**args,'conversation_id':cid,'request_id':req},guard_turn=turn_id)
        if name=='request_human':
            with db.transaction() as c:
                if not _current(c,turn_id):return {'error':'回复已取消'}
                return _handoff(c,user,cid,str(args.get('reason',''))[:1000])
    except HTTPException as exc:
        return {'error':exc.detail}
    except (TypeError,ValueError):
        return {'error':'信息不完整或格式不正确，请确认商品、订单及申请内容'}
