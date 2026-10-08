from fastapi import APIRouter, Body, Depends, File, UploadFile
from fastapi.responses import Response
from . import catalog


def build_router(user):
    router=APIRouter(prefix='/api/admin/catalog',tags=['商品维护'])
    @router.post('/products',status_code=201)
    def create(data:dict=Body(...),u=Depends(user)): return catalog.create_product(u,data)
    @router.patch('/products/{pid}')
    def edit(pid:str,data:dict=Body(...),u=Depends(user)): return catalog.edit_product(u,pid,data)
    @router.post('/products/{pid}/skus')
    def variant(pid:str,data:dict=Body(...),u=Depends(user)): return catalog.add_sku(u,pid,data)
    @router.get('/template')
    def template(format:str='xlsx',current:bool=False,u=Depends(user)):
        if format not in ('csv','xlsx'): catalog.service.fail(422,'请选择 Excel 或 CSV 格式')
        content=catalog.template(u,format,current)
        mime='text/csv; charset=utf-8' if format=='csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return Response(content,media_type=mime,headers={'Content-Disposition':f'attachment; filename="products.{format}"'})
    @router.post('/imports/preview')
    async def preview(file:UploadFile=File(...),u=Depends(user)):
        catalog.service.require_staff(u)
        return catalog.preview_import(u,file.filename or '',await file.read(2*1024*1024+1))
    @router.post('/imports/{iid}/confirm')
    def confirm(iid:str,u=Depends(user)): return catalog.confirm_import(u,iid)
    @router.get('/answers')
    def drafts(u=Depends(user)): return catalog.answer_drafts(u)
    @router.post('/products/{pid}/answers')
    def submit(pid:str,data:dict=Body(...),u=Depends(user)): return catalog.submit_answers(u,pid,data)
    @router.post('/answers/{did}/review')
    def review(did:str,data:dict=Body(...),u=Depends(user)): return catalog.review_answers(u,did,data)
    return router
