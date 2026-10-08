import csv
import io
import json
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response
from . import logistics_operations as logistics, service


def build_router(user_dependency):
    router = APIRouter(prefix='/api/staff')

    @router.get('/shipments/orders')
    def orders(u=Depends(user_dependency)):
        return logistics.available_orders(u)

    @router.get('/shipments/import-template')
    def template(format: str = 'xlsx', u=Depends(user_dependency)):
        service.require_staff(u)
        if format == 'csv':
            output = io.StringIO(); csv.writer(output).writerow(list(logistics.HEADERS))
            return Response(output.getvalue().encode('utf-8-sig'), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="logistics-template.csv"'})
        if format != 'xlsx': logistics._fail(422, '请选择表格或 CSV 模板')
        from openpyxl import Workbook
        wb = Workbook(); sheet = wb.active; sheet.title = '物流资料'; sheet.append(list(logistics.HEADERS))
        for column in ['A', 'C', 'I']:
            for row in range(2, logistics.MAX_ROWS + 2): sheet[f'{column}{row}'].number_format = '@'
        for column in sheet.columns: sheet.column_dimensions[column[0].column_letter].width = 23
        output = io.BytesIO(); wb.save(output); wb.close()
        return Response(output.getvalue(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename="logistics-template.xlsx"'})

    @router.get('/shipments')
    def shipments(q: str = '', order_id: str | None = None, source_kind: str = 'all', exception_only: bool = False, u=Depends(user_dependency)):
        return logistics.list_shipments(u, q, order_id, source_kind, exception_only)

    @router.post('/shipments/import-preview')
    async def preview(request: Request, u=Depends(user_dependency)):
        service.require_staff(u)
        if 'multipart/form-data' in request.headers.get('content-type', ''):
            form = await request.form(); file = form.get('file')
            if file is None or not hasattr(file, 'read'): logistics._fail(422, '请选择标准物流表格')
            content = await file.read(logistics.MAX_BYTES + 1)
            rows, fingerprint = logistics.parse_file(file.filename or '', content)
            try: metadata = json.loads(str(form.get('metadata', '{}')))
            except (ValueError, TypeError): logistics._fail(422, '表格资料核对信息无效，请重新预览')
            if not isinstance(metadata, dict): logistics._fail(422, '请填写有效的资料核对信息')
            return logistics.import_preview(u, metadata | {'rows': rows}, fingerprint)
        if int(request.headers.get('content-length') or 0) > logistics.MAX_BYTES: logistics._fail(422, '资料超过 5 MB，请分批导入')
        try: data = await request.json()
        except (ValueError, TypeError): logistics._fail(422, '请填写有效的物流资料')
        return logistics.import_preview(u, data)

    @router.post('/shipments/import-confirm')
    def confirm(data: dict = Body(...), u=Depends(user_dependency)):
        return logistics.import_confirm(u, data)

    @router.get('/shipments/{identity}')
    def shipment(identity: str, u=Depends(user_dependency)):
        return logistics.get_shipment(u, identity)

    @router.patch('/shipments/{identity}')
    def update_shipment(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return logistics.patch_shipment(u, identity, data)

    @router.patch('/shipment-events/{identity}')
    def event(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return logistics.correct_event(u, identity, data)

    @router.get('/logistics-rules')
    def rules(u=Depends(user_dependency)):
        return logistics.get_rules(u)

    @router.patch('/logistics-rules')
    def update_rules(data: dict = Body(...), u=Depends(user_dependency)):
        return logistics.patch_rules(u, data)

    @router.get('/logistics-exceptions')
    def exceptions(status: str = 'open', source_kind: str = 'all', q: str = '', u=Depends(user_dependency)):
        return logistics.list_exceptions(u, status, source_kind, q)

    @router.post('/logistics-exceptions/recheck')
    def recheck(data: dict = Body(...), u=Depends(user_dependency)):
        return logistics.recheck(u, data)

    @router.post('/logistics-exceptions/{identity}/actions')
    def action(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return logistics.exception_action(u, identity, data)

    return router
