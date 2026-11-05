# 모아타(MOATA) 배포용 이미지. Render·Fly.io·Cloud Run 어디서나 같은 이미지로 돈다.
# SQLite 파일은 영구 디스크(/data)에 두어 재시작해도 남게 한다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/moata.db \
    COOKIE_SECURE=true \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"

# 주기 작업(마감 처리·AI 자동 매칭·배차·시간별 요약)이 서버 안에서 돌므로 워커는 1개로 둔다
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
