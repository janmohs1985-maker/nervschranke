FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py model.py checks.py stats.py statistik.html index.html core.js admin.html login.html impressum.html datenschutz.html ./
COPY static ./static
VOLUME /data
EXPOSE 80
# --no-access-log: keine Zugriffsprotokolle mit IP-Adressen (siehe Datenschutzerklärung)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
