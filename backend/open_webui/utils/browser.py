from fastapi import HTTPException
from open_webui.utils.auth import create_token, decode_token, get_verified_user_by_id


def create_browser_token(
    server_id, user_id, instance_user_id, chat_id, session, headers=None, cookies=None, *, resource='browser'
):
    from open_webui.utils.oauth import encrypt_data

    relay = encrypt_data(
        {
            'authorization': (headers or {}).get('Authorization'),
            'cookies': cookies or {},
        }
    )
    return create_token(
        {
            'typ': 'browser_view',
            'server': server_id,
            'user': user_id,
            'instance_user': instance_user_id,
            'chat': chat_id,
            'session': session['id'],
            'resource': resource,
            'model': (headers or {}).get('X-Terminal-Model-Id'),
            'capability': session['token'],
            'relay': relay,
        },
    )


async def browser_access(server_id, sid, browser_token, request=None):
    data = decode_token(browser_token)
    if not data or data.get('typ') != 'browser_view' or data.get('server') != server_id or data.get('session') != sid:
        raise HTTPException(401, 'Browser session expired')
    user = await get_verified_user_by_id(data.get('user'))
    if user is None:
        raise HTTPException(401, 'Browser session expired')
    if server_id == 'managed':
        from open_webui.utils.auth import get_optional_verified_user_from_request
        from open_webui.utils.sandboxes import workspace_scope
        from fastapi import Request, WebSocket

        if isinstance(request, WebSocket):
            request = Request({**request.scope, 'type': 'http', 'method': 'GET'})
        current = await get_optional_verified_user_from_request(request) if request is not None else None
        if current is None or current.id != user.id:
            raise HTTPException(403, 'Open this browser in its owner session')
        await workspace_scope(current, {'chat_id': data.get('chat', '')})
    from open_webui.utils.oauth import decrypt_data

    data['relay'] = decrypt_data(data['relay'])
    return user, data
