from fastapi import APIRouter, Body, Depends
from . import operations_dashboard as dashboard


def build_router(user_dependency):
    router = APIRouter(prefix='/api/staff')

    @router.get('/operations-observations/context')
    def context(conversation_id: str | None = None, u=Depends(user_dependency)):
        return dashboard.observation_context(u, conversation_id)

    @router.get('/operations-observations/records')
    def original(type: str, id: str, u=Depends(user_dependency)):
        return dashboard.original_record(u, type, id)

    @router.get('/repeat-contacts')
    def repeats(u=Depends(user_dependency)):
        return dashboard.list_ledger(u, 'repeat')

    @router.post('/repeat-contacts')
    def create_repeat(data: dict = Body(...), u=Depends(user_dependency)):
        return dashboard.create_repeat(u, data)

    @router.patch('/repeat-contacts/{identity}')
    def patch_repeat(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return dashboard.patch_repeat(u, identity, data)

    @router.get('/quality-reviews')
    def reviews(u=Depends(user_dependency)):
        return dashboard.list_ledger(u, 'quality')

    @router.post('/quality-reviews')
    def create_quality(data: dict = Body(...), u=Depends(user_dependency)):
        return dashboard.create_quality(u, data)

    @router.patch('/quality-reviews/{identity}')
    def patch_quality(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return dashboard.patch_quality(u, identity, data)

    @router.get('/work-time-entries')
    def times(u=Depends(user_dependency)):
        return dashboard.list_ledger(u, 'time')

    @router.post('/work-time-entries')
    def create_time(data: dict = Body(...), u=Depends(user_dependency)):
        return dashboard.create_time(u, data)

    @router.patch('/work-time-entries/{identity}')
    def patch_time(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return dashboard.patch_time(u, identity, data)

    @router.get('/operations-metrics')
    def metrics(start: str | None = None, end: str | None = None, assignee: str = 'all', include_demo: bool = False, u=Depends(user_dependency)):
        return dashboard.metrics(u, start, end, assignee, include_demo)

    @router.get('/operations-metrics/details')
    def details(snapshot_token: str, metric: str, page: int = 1, page_size: int = 25, u=Depends(user_dependency)):
        return dashboard.metric_details(u, snapshot_token, metric, page, page_size)

    return router
