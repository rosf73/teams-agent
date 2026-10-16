#!/bin/sh
# /usr/local/bin/devtunnel. 진짜 CLI 를 항상 $HOME 에서 실행한다.
#
# devtunnel 은 설정(DevTunnels/devtunnels.json)을 HOME 이 아니라 **현재 디렉터리** 아래에 쓴다
# (2026-10-16 확인). 루트 파일 시스템이 읽기 전용이라 /app 에서 실행하면
# "Read-only file system : '/app/DevTunnels'" 로 실패하고, 로그인 정보도 볼륨에 남지 않는다.
# `kubectl exec … devtunnel user login -d` 처럼 어디서 불러도 같은 위치를 쓰게 여기서 맞춘다.
mkdir -p "$HOME" && cd "$HOME" || exit 1
exec /usr/local/lib/devtunnel/devtunnel "$@"
