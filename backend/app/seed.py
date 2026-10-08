import json
import re
from datetime import datetime, timedelta, timezone
from .db import transaction, now


def js(value):
    return json.dumps(value, ensure_ascii=False)


USERS = [
    {'id':'customer-lin','name':'林小满','role':'customer','description':'喜欢数码与日常好物 · 有耳机、水壶订单','avatar':'林'},
    {'id':'customer-zhou','name':'周予安','role':'customer','description':'在意穿着舒适 · 可体验尺码更换','avatar':'周'},
    {'id':'customer-chen','name':'陈知夏','role':'customer','description':'经营自己的小家 · 可体验破损补发','avatar':'陈'},
    {'id':'customer-xu','name':'许初','role':'customer','description':'第一次来逛逛 · 暂无购买记录','avatar':'许'},
    {'id':'staff','name':'店铺客服','role':'staff','description':'人工审核、接管对话与经营资料','avatar':'好'},
]

# The specs and prices below are fictional store data, inspired by source field categories.
CATALOG = [
 ('E1','轻听降噪耳机','E1','数码',29900,'日常通勤少一点喧闹，多一点自己的节奏。',{'降噪':'主动降噪','佩戴':'入耳式','续航':'单次 8 小时 / 含充电盒 32 小时','连接':'蓝牙 5.3','防护':'日常防泼溅，请勿水洗'},['WH','BK']),
 ('K1','日常电热水壶','K1','家电',16900,'一壶刚好的热水，照顾每个平常的早晨。',{'容量':'1.5 L','功率':'1500 W','功能':'一键煮沸，自动断电','材质':'304 不锈钢内胆','保温':'不支持保温'},['WH','SG']),
 ('T2','松弛感纯棉短袖','T2','服饰',12900,'留一点舒适的余量，穿过四季的基础款。',{'面料':'100% 棉','版型':'宽松落肩','护理':'低于 30°C 轻柔洗涤，自然晾干','厚度':'中等，适合春夏'},['WH','BG']),
 ('M1','晨间陶瓷杯','M1','家居',6900,'把生活的温度，轻轻握在手里。',{'容量':'350 mL','材质':'釉面陶瓷','适用':'可微波加热，可用洗碗机','注意':'避免骤冷骤热'},['WH','SG']),
 ('E0','轻听无线耳机','E0','数码',19900,'轻巧好听，适合随身的音乐时光。',{'降噪':'无主动降噪，支持通话降噪','佩戴':'半入耳式','续航':'单次 6 小时 / 含充电盒 24 小时','连接':'蓝牙 5.3'},['WH','BK']),
 ('E1P','轻听 Pro 降噪耳机','E1 Pro','数码',49900,'更安静的通勤，也有从容切换的双设备连接。',{'降噪':'自适应主动降噪','佩戴':'入耳式','续航':'单次 10 小时 / 含充电盒 40 小时','连接':'蓝牙 5.4，双设备连接','充电':'USB-C / 无线充电'},['WH','BK']),
 ('K1P','自在恒温电水壶','K1 Pro','家电',29900,'从温水到热茶，都有合适的温度。',{'容量':'1.5 L','功率':'1500 W','功能':'40–90°C 五档温控，12 小时保温','材质':'304 不锈钢内胆','安全':'防干烧，自动断电'},['WH','SG']),
 ('F1','轻风桌面风扇','F1','家电',18900,'给书桌留一缕舒服的风。',{'档位':'四档风量','摇头':'左右 60°','供电':'USB-C，内置电池','续航':'低档约 12 小时','清洁':'断电后拆洗前网罩'},['WH','BK']),
 ('T1','日常纯棉短袖','T1','服饰',9900,'干净的线条，刚刚好的合身。',{'面料':'100% 棉','版型':'常规直筒','护理':'低于 30°C 轻柔洗涤','厚度':'轻薄'},['WH','BK']),
 ('T3','柔软纯棉长袖','T3','服饰',15900,'柔软贴身，换季时的一份安心。',{'面料':'100% 棉','版型':'常规直筒','护理':'低于 30°C 轻柔洗涤','厚度':'中等，适合春秋'},['WH','BK']),
 ('G1','清透耐热玻璃杯','G1','家居',4900,'看得见茶色，也装得下日常。',{'材质':'高硼硅玻璃','适用':'热饮 / 冷饮','注意':'避免碰撞与骤冷骤热','护理':'温和清洁，请勿使用钢丝球'},['270','360']),
 ('M2','悠然早餐杯','M2','家居',8900,'一杯热牛奶，让早晨慢下来。',{'容量':'450 mL','材质':'釉面陶瓷','适用':'可微波加热，可用洗碗机','配件':'杯体一个，不含杯盖'},['WH','SG']),
]
COLORS = {'WH':'暖白','BK':'曜石黑','SG':'鼠尾草绿','BG':'燕麦色','270':'270 mL','360':'360 mL'}
SOURCES = {'数码':'https://checkout-ca.nothing.tech/products/ear-a', '家电':'https://www.mi.com/sg/product/xiaomi-smart-kettle-2-pro/specs/', '服饰':'https://www.uniqlo.com/uk/en/products/E465185-000/00?colorDisplayCode=00', '家居':'https://www.ikea.cn/cn/zh/p/60278368/'}


def seed():
    with transaction() as c:
        if c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            return
        for user in USERS:
            c.execute('INSERT INTO users VALUES (?,?)', (user['id'],js(user)))
        stamp = now()
        for pid,name,model,category,price,description,specs,colors in CATALOG:
            size_chart = {'S':{'胸围_cm':100,'衣长_cm':66},'M':{'胸围_cm':106,'衣长_cm':69},'L':{'胸围_cm':112,'衣长_cm':72}} if category=='服饰' else None
            if pid=='T2':
                size_chart = {'S':{'胸围_cm':110,'衣长_cm':66},'M':{'胸围_cm':116,'衣长_cm':69},'L':{'胸围_cm':122,'衣长_cm':72}}
            product = {'id':pid,'name':name,'model':model,'category':category,'description':description,'image':f'/assets/{pid.lower()}.png','price_cents':price,'aliases':[model,pid,name, '耳塞' if category=='数码' else name[:2]],'specs':specs,'package_contents':['商品本体 × 1','使用说明 × 1'] + (['充电盒 × 1','USB-C 线 × 1'] if category=='数码' else []),'faq':[{'q':'这是真实品牌商品吗？','a':'这是日常好物的虚拟演示商品，参数、价格和库存均为演示设定。'},{'q':'可以退换吗？','a':'可以先提交申请，普通商品支持七日无理由退货；质量问题单独受理，由人工核实并决定。'}],'size_chart':size_chart,'source_url':('https://www.mi.com/sg/product/mi-smart-standing-fan-2-lite/specs/' if pid=='F1' else 'https://www.ikea.cn/cn/zh/p/pokal-bo-ke-er-bei-zi-tou-ming-bo-li-70288239/' if pid=='G1' else SOURCES[category]),'source_note':'仅参考官方资料的参数字段结构；本商品名称、参数、价格、库存均为虚拟演示设定。','retrieved_at':stamp}
            c.execute('INSERT INTO products VALUES (?,?)',(pid,js(product)))
            for color in colors:
                for size in (['S','M','L'] if category=='服饰' else ['']):
                    sid = '-'.join(filter(None,[pid,color,size]))
                    stock = {'E1-BK':3,'K1P-SG':1,'E1P-WH':0,'T2-BG-M':0,'G1-360':0}.get(sid,20)
                    restock = (datetime.now(timezone.utc)+timedelta(days=3)).isoformat() if sid=='E1P-WH' else None
                    label = ' / '.join(filter(None,[COLORS[color],size]))
                    c.execute('INSERT INTO skus VALUES (?,?,?,?,?,?,?,?,?)',(sid,pid,label,COLORS[color],size,price,stock,restock,1))
        policy={'version':1,'return_days':7,'exchange_days':7,'no_reason_shipping':'customer','quality_note':'质量问题单独受理，不因超过七天自动拒绝；由人工结合照片、订单和实际情况核实。','extra_note':'同款等价规格可申请换货；破损或少件可申请补发。部分退货保留剩余商品已享优惠，合理拆封查验不自动判定不可退。所有申请由人工决定并记录完成。','published_at':stamp}
        promos={'version':1,'threshold_cents':19900,'reduction_cents':2000,'special_product_id':'E1P','special_price_cents':42900,'starts_at':(datetime.now(timezone.utc)-timedelta(days=30)).isoformat(),'ends_at':(datetime.now(timezone.utc)+timedelta(days=365)).isoformat(),'enabled':True}
        c.execute('INSERT INTO policies VALUES (?,?)',(1,js(policy)))
        c.execute('INSERT INTO promotions VALUES (1,?)',(js(promos),))
        orders=[('DEMO-LIN-01','customer-lin','E1-WH',1,3),('DEMO-LIN-02','customer-lin','K1-WH',1,12),('DEMO-ZHOU-01','customer-zhou','T2-WH-M',2,2),('DEMO-ZHOU-02','customer-zhou','T1-BK-L',1,10),('DEMO-CHEN-01','customer-chen','M1-WH',2,1),('DEMO-CHEN-02','customer-chen','G1-270',1,4)]
        for oid,user,sid,quantity,days in orders:
            sku=dict(c.execute('SELECT * FROM skus WHERE id=?',(sid,)).fetchone())
            product=json.loads(c.execute('SELECT data FROM products WHERE id=?',(sku['product_id'],)).fetchone()[0])
            subtotal=sku['price_cents']*quantity
            discount=2000 if subtotal>=19900 else 0
            delivered=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
            order={'id':oid,'user_id':user,'sku_id':sid,'product_id':sku['product_id'],'product_name':product['name'],'sku_label':sku['label'],'image':product['image'],'quantity':quantity,'unit_price_cents':sku['price_cents'],'discount_cents':discount,'total_cents':subtotal-discount,'status':'delivered','created_at':(datetime.now(timezone.utc)-timedelta(days=days+2)).isoformat(),'delivered_at':delivered,'policy_version':1,'remaining_quantity':quantity}
            c.execute('INSERT INTO orders VALUES (?,?,?)',(oid,user,js(order)))


def migrate():
    """Additive v2 upgrade; never reseed existing stock, users' orders or cases."""
    with transaction() as c:
        if c.execute('SELECT 1 FROM schema_migrations WHERE version=2').fetchone():
            return
        user = next(u for u in USERS if u['id'] == 'customer-xu')
        c.execute('INSERT OR IGNORE INTO users VALUES (?,?)', (user['id'], js(user)))
        parts_by_product = {}
        previous_contents = {}
        for row in c.execute('SELECT id,data FROM products').fetchall():
            p = json.loads(row['data']); pid = p['id']
            # Keep the pre-upgrade catalogue separate from today's expanded
            # package. An added part is not evidence that an old order had it.
            previous_contents[pid] = list(p.get('package_contents', []))
            parts = []
            if p['category'] == '数码':
                parts = [('CASE', '充电盒'), ('CABLE', 'USB-C 线')]
            elif pid in ('K1', 'K1P'):
                parts = [('BASE', '供电底座')]
            elif pid == 'F1':
                parts = [('CABLE', 'USB-C 线')]
            items = [{'id': pid + '-MAIN', 'name': '商品本体', 'kind': 'main', 'quantity': 1}]
            for code, name in parts:
                sid = pid + '-PART-' + code
                c.execute('INSERT OR IGNORE INTO part_skus(id,product_id,name,stock) VALUES (?,?,?,?)', (sid, pid, name, 20))
                items.append({'id': sid, 'name': name, 'kind': 'part', 'quantity': 1, 'part_sku_id': sid})
                if not any(name in s for s in p.get('package_contents', [])):
                    p.setdefault('package_contents', []).append(name + ' × 1')
            p['package_items'] = items
            parts_by_product[pid] = items
            c.execute('UPDATE products SET data=? WHERE id=?', (js(p), pid))
        for row in c.execute('SELECT id,data FROM orders').fetchall():
            order = json.loads(row['data'])
            if 'package_snapshot' not in order:
                purchase_contents = order.get('package_contents')
                contents = purchase_contents if isinstance(purchase_contents, list) else previous_contents.get(order['product_id'], [])
                normalized = [''.join(str(value).split()).lower() for value in contents]
                reconstructed = []
                for item in parts_by_product.get(order['product_id'], []):
                    if item['kind'] == 'main':
                        reconstructed.append(item)
                        continue
                    name = ''.join(item['name'].split()).lower()
                    entry = next((value for value in normalized if name in value and not re.search(r'不含|不包含|不附|不配|未含|无需', value)), None)
                    if entry is not None:
                        count = re.search(r'(?:×|x|\*)(\d+)', entry)
                        quantity = int(count.group(1)) if count else 1
                        if quantity > 0:reconstructed.append({**item, 'quantity': quantity})
                order['package_snapshot'] = reconstructed
                order['package_snapshot_source'] = ('legacy_purchase_contents_reconstruction'
                    if isinstance(purchase_contents, list) else 'legacy_catalog_reconstruction')
            c.execute('UPDATE orders SET data=? WHERE id=?', (js(order), row['id']))
        old = json.loads(c.execute('SELECT data FROM policies ORDER BY version DESC LIMIT 1').fetchone()[0])
        policy = {**old, 'version': old['version'] + 1, 'return_days': max(7, old['return_days']),
                  'exchange_days': max(15, old['exchange_days']), 'quality_days': 30, 'reship_days': 30,
                  'warranty_days': 365, 'engine_version': '2026-09-balanced-v2', 'published_at': now(),
                  'quality_note': '店铺承诺三十日质量退换，数码家电一年保修；质量问题不因超过七天或三十天自动拒绝，依法应有的救济另行核实。',
                  'extra_note': '店铺承诺七日无理由退货、十五日非质量同款等价换货。整件和购买时清单内配件可补发；缺货不影响资格。AI仅建议，人工终审与执行。'}
        c.execute('INSERT INTO policies VALUES (?,?)', (policy['version'], js(policy)))
        c.execute('INSERT OR IGNORE INTO case_notices SELECT user_id,order_id,\'return_refund\',first_at FROM return_notices')
        c.execute('INSERT INTO schema_migrations VALUES (?,?)', (2, now()))
