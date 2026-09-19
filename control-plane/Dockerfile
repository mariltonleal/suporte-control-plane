FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY control-plane/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd -u 10002 -m control
COPY control-plane/ /app/
RUN mkdir -p /targets && chown -R control:control /targets
USER control
EXPOSE 8080
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8080","--proxy-headers","--forwarded-allow-ips=*"]
