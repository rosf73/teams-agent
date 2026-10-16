# 사내 Kubernetes 용 이미지 (docs/K8S-HANDOFF.md). 봇 2개와 devtunnel CLI 를 한 이미지에 넣고,
# Deployment 에서 컨테이너마다 command 만 바꿔 쓴다.
# 비밀값은 넣지 않는다. .dockerignore 가 허용 목록 방식이라 .env 는 빌드 컨텍스트에 들어오지 않는다.
FROM python:3.13-slim

# devtunnel 은 .NET 프로그램이라 ICU 가 없으면 시작하자마자 죽는다(2026-10-16 확인).
# 패키지 이름에 버전이 붙는다(trixie = libicu76). 베이스가 바뀌면 여기서 빌드가 실패하니 맞춰 고친다.
RUN apt-get update && apt-get install -y --no-install-recommends libicu76 \
    && rm -rf /var/lib/apt/lists/*

# 두 봇의 requirements.txt 는 같다. 하나만 설치한다.
COPY vote-agent/src/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt \
    && useradd --uid 10001 --create-home app

# Microsoft 공식 배포 주소. 노드가 amd64 라 linux-x64 만 받는다.
# PATH 의 devtunnel 은 $HOME 으로 이동해서 실행하는 래퍼다 (deploy/devtunnel-shim.sh 참고).
ADD --chmod=755 https://aka.ms/TunnelsCliDownload/linux-x64 /usr/local/lib/devtunnel/devtunnel
COPY --chmod=755 deploy/devtunnel-shim.sh /usr/local/bin/devtunnel

WORKDIR /app
COPY roulette-agent/src/ roulette-agent/src/
COPY vote-agent/src/ vote-agent/src/
COPY --chmod=755 deploy/tunnel-entry.sh /usr/local/bin/tunnel-entry.sh

# 루트 파일 시스템이 읽기 전용이라 .pyc 를 쓰지 않는다.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
