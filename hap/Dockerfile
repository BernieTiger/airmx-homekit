FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY hap/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin hap \
 && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data

COPY hap ./hap

USER 10001:10001

# 51826 = HAP；注意必须用 host 网络，否则 mDNS 广播出不了容器
EXPOSE 51826

CMD ["python", "-m", "hap"]
