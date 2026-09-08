"""SDK 가 아직 모르는 activity 를 흘려보내는 호환 계층.

## 왜 필요한가

Teams 앱을 **업데이트**하면 Teams 가 `installationUpdate` 를 `action: "upgrade"` 로 보낸다.
그런데 microsoft-teams-api 2.0.15/2.0.16 의 tagged union 은 `add` / `remove` 만 안다.

    Input tag 'upgrade' found using 'action' does not match
    any of the expected tags: 'add', 'remove'

pydantic 검증이 `app_process.process_activity` 안에서 터지므로 미들웨어·핸들러보다
먼저 실패한다 → **500** → Bot Framework 가 재전송 → 같은 500 반복.

업스트림 main 에는 `InstalledUpgradeActivity` 가 추가됐지만 아직 릴리스되지 않았다
(2.0.16 휠에 `upgrade.py` 가 없는 것을 확인했다).

## 어떻게 막는가

`http_server_adapter` 는 **지원되는 옵션**이다. 우리 FastAPI 인스턴스에 미들웨어를 달아
SDK 가 모델링하지 못하는 `installationUpdate` 는 검증 전에 200 으로 끝낸다.
설치·업데이트 알림은 이 봇이 쓰지 않으므로 버려도 잃는 것이 없다.

**허용 목록을 SDK 에서 읽어온다** — SDK 가 `upgrade` 를 지원하게 되면 이 우회는
자동으로 아무것도 하지 않는다. 손으로 지울 필요가 없다.
"""

from __future__ import annotations

import json
import logging
import typing

from fastapi import FastAPI, Request, Response
from microsoft_teams.api.activities.install_update import InstallUpdateActivity
from microsoft_teams.apps.http.fastapi_adapter import FastAPIAdapter

logger = logging.getLogger(__name__)

MESSAGING_PATH = "/api/messages"


def _supported_install_actions() -> frozenset[str]:
    """SDK 가 모델링한 `installationUpdate` 의 action 값을 모은다.

    union 멤버의 `action` 필드가 `Literal[...]` 이므로 거기서 값을 꺼낸다.
    """
    actions: set[str] = set()
    members = typing.get_args(InstallUpdateActivity)
    for member in members:
        for model in typing.get_args(member) or (member,):
            field = getattr(model, "model_fields", {}).get("action")
            if field is None:
                continue
            actions.update(a for a in typing.get_args(field.annotation) if isinstance(a, str))
    return frozenset(actions)


SUPPORTED_INSTALL_ACTIONS = _supported_install_actions()


def build_adapter() -> FastAPIAdapter:
    """미들웨어를 단 FastAPI 어댑터. `App(http_server_adapter=...)` 에 넘긴다."""
    fastapi_app = FastAPI()

    @fastapi_app.middleware("http")
    async def drop_unmodeled_activities(request: Request, call_next):
        if request.method != "POST" or request.url.path != MESSAGING_PATH:
            return await call_next(request)

        body = await request.body()

        # 본문을 한 번 읽었으므로 스트림을 되돌려 놓아야 SDK 가 다시 읽을 수 있다.
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive  # type: ignore[attr-defined]

        try:
            payload = json.loads(body or b"{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}

        if isinstance(payload, dict) and payload.get("type") == "installationUpdate":
            action = payload.get("action")
            if action not in SUPPORTED_INSTALL_ACTIONS:
                logger.info(
                    "SDK 가 모르는 installationUpdate 무시 (action=%r, 지원=%s)",
                    action, sorted(SUPPORTED_INSTALL_ACTIONS))
                return Response(status_code=200)

        return await call_next(request)

    return FastAPIAdapter(app=fastapi_app)
