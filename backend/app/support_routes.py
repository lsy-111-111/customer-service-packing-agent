from fastapi import APIRouter, Body, Depends
from . import support, work_operations


def build_router(user_dependency):
    router = APIRouter(prefix='/api')

    @router.get('/service-requests')
    def requests(conversation_id: str = '', u=Depends(user_dependency)):
        return support.list_requests(u, conversation_id or None)

    @router.post('/service-requests')
    def create(data: dict = Body(...), u=Depends(user_dependency)):
        return support.create_request(u, data)

    @router.get('/service-requests/{identity}')
    def detail(identity: str, u=Depends(user_dependency)):
        return support.get_request(u, identity)

    @router.post('/service-requests/{identity}/actions')
    def action(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return support.request_action(u, identity, data)

    @router.get('/staff/work-items')
    @router.get('/work-items')
    def work(stage: str = 'all', conversation_id: str = '', assignee: str = 'all',
             deadline_scope: str = 'staff', deadline: str = 'all', q: str = '', u=Depends(user_dependency)):
        return work_operations.work_items(u, stage=stage, conversation_id=conversation_id or None,
                                          assignee=assignee, deadline_scope=deadline_scope, deadline=deadline, q=q)

    @router.get('/staff/service-metrics')
    def metrics(u=Depends(user_dependency)):
        return support.service_metrics(u)

    @router.get('/conversations/{cid}/support')
    def summary(cid: str, u=Depends(user_dependency)):
        return support.conversation_support(u, cid)

    @router.get('/service-feedback')
    @router.get('/feedback')
    def feedback(target_type: str, target_id: str, u=Depends(user_dependency)):
        return support.get_feedback(u, target_type, target_id)

    @router.post('/service-feedback')
    @router.post('/feedback')
    def submit(data: dict = Body(...), u=Depends(user_dependency)):
        return support.submit_feedback(u, data)

    @router.post('/staff/service-quality')
    def quality(data: dict = Body(...), u=Depends(user_dependency)):
        return support.record_quality(u, data)

    @router.patch('/service-feedback/{identity}')
    def feedback_details(identity: str, data: dict = Body(...), u=Depends(user_dependency)):
        return support.update_feedback(u, identity, data)

    return router
