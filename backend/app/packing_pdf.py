"""Chinese trial labels with embedded redistributable typeface and local barcodes."""
import io
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.graphics.barcode.code128 import Code128
from .service import fail

FONT = 'PackingChinese'


def _font():
    if FONT not in pdfmetrics.getRegisteredFontNames():
        path = Path(__file__).parent / 'assets/fonts/NotoSansSC.ttf'
        if not path.exists():
            fail(503, '打印字体暂不可用，请联系管理员补齐中文打印字体')
        try:
            pdfmetrics.registerFont(TTFont(FONT, str(path)))
        except Exception:
            fail(503, '中文打印字体未能加载，请联系管理员后重试')


def render(task, boxes, mode, paper, kind, width_mm, height_mm, pages=None, show_note=True):
    _font()
    output = io.BytesIO()
    width, height = width_mm * mm, height_mm * mm
    page_size = A4 if paper == 'a4' else (width, height)
    pdf = canvas.Canvas(output, pagesize=page_size, pageCompression=1)
    pdf.setTitle('装箱任务 ' + task['number'] + ' - 试用箱唛')
    pdf.setAuthor('日常好物装箱工作台')
    kinds = ['main', 'side', 'internal'] if kind == 'all' else [kind]
    for box in boxes:
        line = task['lines'][box['line_index']]
        for label_kind in kinds:
            x, y = (page_size[0] - width) / 2, (page_size[1] - height) / 2
            pdf.setStrokeColorRGB(.2, .25, .24)
            pdf.setLineWidth(.7)
            pdf.roundRect(x, y, width, height, 3 * mm)
            pdf.setFillColorRGB(.1, .2, .18)
            pdf.setFont(FONT, 15)
            title = {'main': '正唛 · 发货资料', 'side': '侧唛 · 装箱明细', 'internal': '内部核验标签'}[label_kind]
            pdf.drawString(x + 7 * mm, y + height - 12 * mm, title)
            pdf.setFont(FONT, 8)
            pdf.drawString(x + 7 * mm, y + height - 19 * mm, '试用样张，仓库要求待核对')
            pdf.setFont(FONT, 11)
            pdf.drawRightString(x + width - 7 * mm, y + height - 29 * mm, f'第 {box["number"]} 箱 / 共 {task["total_boxes"]} 箱')
            if mode == 'draft':
                pdf.setFillColorRGB(.68, .24, .12)
                pdf.drawString(x + 7 * mm, y + height - 29 * mm, '草稿 · 不可发货')
            else:
                pdf.drawString(x + 7 * mm, y + height - 29 * mm, '本地审核已通过')
            pdf.setFillColorRGB(.1, .15, .14)
            if label_kind == 'main':
                rows = [('装箱任务', task['number']), ('发货单位', task['supplier'] or '待填写'),
                        ('收货仓库', task['destination'] or '待填写'), ('计划发货', task['ship_date'] or '待填写'),
                        ('商品名称', line['product_name']), ('商品规格', line['specification']),
                        ('本箱数量', str(box['planned_quantity']) + ' 件'), ('平台单号', task['platform_order'] or '未提供')]
                if line.get('external_code'):
                    rows.insert(4, ('客户商品编码', line['external_code']))
            elif label_kind == 'side':
                kit = line['kit_note'] or '；'.join(f'{item["name"]} × {item["quantity"]}' for item in line.get('package_items', [])) or '未提供'
                rows = [('装箱任务', task['number']), ('商品名称', line['product_name']), ('商品规格', line['specification']),
                        ('计划 / 实装', f'{box["planned_quantity"]} / ' + (str(box['actual_quantity']) if box['actual_quantity'] is not None else '待记录') + ' 件'),
                        ('批次', line['batch'] or '未提供'), ('生产 / 到期', (line['produced_at'] or '未提供') + ' / ' + (line['expires_at'] or '未提供')),
                        ('净重 / 毛重', f'{box["net_weight"] if box["net_weight"] is not None else "未称重"} / {box["gross_weight"] if box["gross_weight"] is not None else "未称重"} 公斤'),
                        ('套装及配件', kit), ('序列号追溯', f'{len(box["serial_numbers"])} 个，详见装箱记录及随箱资料' if box['serial_numbers'] else '未要求或未提供')]
            else:
                rows = [('装箱任务', task['number']), ('商品名称', line['product_name']), ('商品规格', line['specification']),
                        ('本箱数量', str(box['planned_quantity']) + ' 件'), ('批次', line['batch'] or '未提供'),
                        ('核验用途', '供本店员工在装箱工作台扫描；不作为平台或仓库收货条码。')]
            style = ParagraphStyle('packing', fontName=FONT, fontSize=9, leading=13, wordWrap='CJK', textColor='#192a25')
            top = y + height - 37 * mm
            bottom = y + (39 if label_kind == 'internal' else 19) * mm
            content = '<br/>'.join(f'<b>{escape(title)}：</b>{escape(str(value)).replace(chr(10), " ")}' for title, value in rows)
            paragraph = Paragraph(content, style)
            _, used = paragraph.wrap(width - 14 * mm, top - bottom)
            if used > top - bottom:
                fail(422, '所选标签容纳不下完整资料，请缩短备注或选择更大的标签尺寸')
            paragraph.drawOn(pdf, x + 7 * mm, top - used)
            if label_kind == 'internal':
                if mode == 'formal':
                    barcode = Code128(box['code'], barHeight=14 * mm, barWidth=.27 * mm, quiet=True)
                    if barcode.width > width - 12 * mm:
                        fail(422, '内部条码放不下，请增大标签宽度')
                    barcode.drawOn(pdf, x + (width - barcode.width) / 2, y + 18 * mm)
                    pdf.setFont(FONT, 9)
                    pdf.drawCentredString(x + width / 2, y + 13 * mm, box['code'])
                else:
                    pdf.setFont(FONT, 10)
                    pdf.drawCentredString(x + width / 2, y + 23 * mm, '草稿没有可用核验条码')
            pdf.setFont(FONT, 6.5)
            pdf.drawString(x + 7 * mm, y + 7 * mm, '打印比例 100% · 试用尺寸，不代表京东官方标准')
            if paper == 'a4':
                pdf.setFont(FONT, 9)
                pdf.drawCentredString(page_size[0] / 2, y - 9 * mm, f'标签尺寸：{width_mm} × {height_mm} 毫米，请沿边框裁切')
            if pages is not None:
                pages.append({'page': pdf.getPageNumber(), 'box_number': box['number'], 'kind': label_kind})
            pdf.showPage()
        # Serial numbers and supplied product barcode remain separate from the internal code.
        # A complete attachment is emitted when these supplied details exist, never silently truncated.
        if line['product_barcode'] or box['serial_numbers'] or (show_note and task['note']):
            x = 8 * mm
            top = page_size[1] - 12 * mm
            pdf.setFont(FONT, 12)
            pdf.drawString(x, top, f'第 {box["number"]} 箱 · 随箱补充资料')
            text = [('装箱任务', task['number']), ('提供的商品条码', line['product_barcode'] or '未提供'),
                    ('序列号', '、'.join(box['serial_numbers']) or '未提供'), ('发货备注', (task['note'] or '无') if show_note else '按已审核版式不打印备注'),
                    ('提示', '试用样张，仓库要求待核对。商品条码为填写人提供，尚未验证平台要求。')]
            style = ParagraphStyle('appendix', fontName=FONT, fontSize=9, leading=13, wordWrap='CJK')
            para = Paragraph('<br/>'.join(f'{escape(k)}：{escape(v)}' for k, v in text), style)
            avail = page_size[1] - 30 * mm
            chunks = [para]
            while chunks:
                current = chunks.pop(0)
                _, h = current.wrap(page_size[0] - 16 * mm, avail)
                if h > avail:
                    split = current.split(page_size[0] - 16 * mm, avail)
                    if not split:
                        fail(422, '补充资料过长，请拆分装箱任务')
                    current, *rest = split
                    chunks = rest + chunks
                    _, h = current.wrap(page_size[0] - 16 * mm, avail)
                current.drawOn(pdf, x, top - 7 * mm - h)
                if pages is not None:
                    pages.append({'page': pdf.getPageNumber(), 'box_number': box['number'], 'kind': 'appendix'})
                pdf.showPage()
    pdf.save()
    return output.getvalue()
