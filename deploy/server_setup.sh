#!/usr/bin/env bash
# 서버(Ubuntu)에서 실행: 앱을 systemd 서비스로 올리고, 공인IP가 주어지면 Caddy로 HTTPS(<IP>.sslip.io) 구성.
# 사용법(서버에서):
#   sudo APP_PUBLIC_IP=<공인IP> bash /opt/carecall/deploy/server_setup.sh
# 사전: PC에서 프로젝트를 /opt/carecall 로 전송하고 .env 를 함께 넣어둘 것.
set -euo pipefail

APP_DIR=/opt/carecall
PUBLIC_IP="${APP_PUBLIC_IP:-}"
cd "$APP_DIR"

echo "== [1/4] 의존성 설치 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip curl

echo "== [2/4] venv + requirements =="
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt

echo "== [2.5/4] RAG 인덱스 빌드 (실 키가 .env에 있으면 real 임베딩) =="
./.venv/bin/python build_index.py --source fixtures
# 주 1회 자동 갱신(신선도) — 이미 등록돼 있으면 중복 추가하지 않음
CRON_LINE="10 4 * * 1 cd $APP_DIR && ./.venv/bin/python build_index.py --source fixtures >> /var/log/carecall-index.log 2>&1 && curl -s -X POST http://127.0.0.1:8080/api/rag/reload > /dev/null"
# I4: 빈 crontab이면 grep이 매치 0으로 exit 1 → set -e에 걸려 첫 배포가 중단되므로 || true로 흡수
( crontab -l 2>/dev/null | grep -vF "build_index.py" || true ; echo "$CRON_LINE" ) | crontab -

echo "== [3/4] systemd 서비스 등록 =="
cp deploy/carecall.service /etc/systemd/system/carecall.service
systemctl daemon-reload
systemctl enable --now carecall
sleep 3
systemctl --no-pager --lines=5 status carecall || true
echo "-- health --"; curl -s http://127.0.0.1:8080/health || true; echo

if [ -z "$PUBLIC_IP" ]; then
  echo "== [4/4] Caddy 생략 (APP_PUBLIC_IP 미지정). HTTP: http://<IP>:8080 =="
  exit 0
fi

echo "== [4/4] Caddy 자동 HTTPS ($PUBLIC_IP.sslip.io) =="
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
# I5: --yes로 기존 keyring 덮어쓰기 허용(재실행 멱등 — 없으면 "File exists. Overwrite?"에서 멈춤)
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y
apt-get install -y caddy
DOMAIN="${PUBLIC_IP}.sslip.io"
printf '%s {\n    reverse_proxy 127.0.0.1:8080\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile
systemctl restart caddy
sleep 2
echo "DONE."
echo "  HTTP : http://${PUBLIC_IP}:8080"
echo "  HTTPS: https://${DOMAIN}   (모바일 마이크는 이 https 주소에서 동작)"
