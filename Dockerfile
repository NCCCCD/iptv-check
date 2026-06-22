FROM python:3.11-alpine

RUN apk add --no-cache tzdata

WORKDIR /app

COPY iptv-check.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV TZ=Asia/Shanghai
ENV CRON_SCHEDULE="0 3 * * *"

ENTRYPOINT ["/entrypoint.sh"]
