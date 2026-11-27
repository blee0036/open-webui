from fastapi import APIRouter, Depends
from open_webui.utils.auth import get_verified_user
from open_webui.utils.sandboxes import controller_request
from pydantic import BaseModel

router = APIRouter()


class WorkspaceControl(BaseModel):
    action: str
    confirm: str | None = None


@router.get('/{project_id}')
async def status(project_id: str, user=Depends(get_verified_user)):
    return await controller_request(user, project_id)


@router.post('/{project_id}')
async def control(project_id: str, form: WorkspaceControl, user=Depends(get_verified_user)):
    result = await controller_request(user, project_id, 'POST', form.model_dump())
    if form.action in {'rescan', 'clear'}:
        from open_webui.utils.storage_quota import forget_sandbox_disk

        forget_sandbox_disk(user.id)  # The quota badge should see the cleanup right away.
    return result
