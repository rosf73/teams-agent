import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    """봇 설정.

    CLIENT_ID / CLIENT_SECRET / TENANT_ID 는 SDK(microsoft_teams.apps.App)가
    환경변수에서 직접 읽으므로 여기서 App 에 넘길 필요가 없다.
    이 클래스는 사람이 읽을 값과 로그용 값만 들고 있다.
    """

    AGENT_NAME = "당첨자뽑기"

    # Dev Tunnel 의 포트와 반드시 일치해야 한다.
    PORT = int(os.environ.get("PORT", "3978"))

    # 로그/디버그 용도. 인증 자체는 SDK 가 처리한다.
    CLIENT_ID = os.environ.get("CLIENT_ID", "")
    TENANT_ID = os.environ.get("TENANT_ID", "")
