from __future__ import annotations

import requests


ACCOUNT_SESSION_API_URL = 'https://nathamedia.net/api/veo/account-session'
ACCOUNT_SESSION_TIMEOUT = 30.0


class AccountSessionError(Exception):
    pass


def fetch_account_session_token(
    api_key: str,
    url: str = ACCOUNT_SESSION_API_URL,
    timeout: float = ACCOUNT_SESSION_TIMEOUT,
    current_token: str | None = None,
    project_id: str | None = None,
    force_refresh: bool = False,
) -> dict[str, str]:
    """Fetch one Veo account session token from the backend.

    Returns a dict containing token, project_id, and account_name.
    This helper only performs the API call and validation; it does not
    mutate UI state or scheduler behavior.
    """
    request_body = {}
    if current_token:
        request_body['current_token'] = current_token
        request_body['token'] = current_token
    if project_id:
        request_body['project_id'] = project_id
    if force_refresh:
        request_body['force_refresh'] = True

    response = requests.post(
        url,
        headers={
            'X-API-Key': api_key,
            'Content-Type': 'application/json',
        },
        json=request_body,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()

    if not payload.get('success'):
        raise AccountSessionError(str(payload.get('error') or payload.get('message') or 'Account session API failed'))

    data = payload.get('data') or {}
    token = (data.get('token') or '').strip()
    if not token:
        raise AccountSessionError('Account session API did not return data.token')

    return {
        'token': token,
        'project_id': str(data.get('project_id') or ''),
        'account_name': str(data.get('account_name') or ''),
    }
