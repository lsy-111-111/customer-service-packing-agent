from fastapi import APIRouter, Body, Depends
from . import work_operations as work


def build_router(user_dependency):
    router = APIRouter(prefix='/api/staff')

    @router.get('/operators')
    def operators(include_inactive: bool = False, u=Depends(user_dependency)):
        return work.list_operators(u, include_inactive)

    @router.post('/operators')
    def create_operator(data: dict = Body(...), u=Depends(user_dependency)):
        return work.create_operator(u, data)

    @router.patch('/operators/{identity}')
    def update_operator(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return work.update_operator(u, identity, data)

    @router.get('/work-items/operations')
    def operations(type: str, id: str, u=Depends(user_dependency)):
        return work.get_operations(u, type, id)

    @router.patch('/work-items/operations')
    def update_operations(data: dict = Body(...), u=Depends(user_dependency)):
        return work.patch_operations(u, data)

    @router.get('/work-items/action-templates')
    def templates(type: str, id: str, u=Depends(user_dependency)):
        return work.action_templates(u, type, id)

    @router.post('/work-items/action-preview')
    def preview(data: dict = Body(...), u=Depends(user_dependency)):
        return work.preview_action(u, data)

    @router.post('/work-items/action-confirm')
    def confirm(data: dict = Body(...), u=Depends(user_dependency)):
        return work.confirm_action(u, data)

    return router
