FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends iptables && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY controller.py public_proxy.py firewall.sh ./
RUN chmod 755 /app/firewall.sh
CMD ["uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
