FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway && mkdir /data && chown 10001:10001 /data && chmod 700 /data
COPY airmx_gateway ./airmx_gateway
USER 10001:10001
EXPOSE 8080 8086
CMD ["python", "-m", "airmx_gateway"]
