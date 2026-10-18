FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Which release this image is. Build one image per version, e.g.
#   docker build --build-arg PAYMENTS_VERSION=v2.5.0 -t loki-demo/payments-svc:v2.5.0 services/payments-svc
ARG PAYMENTS_VERSION=v2.4.0
ENV PAYMENTS_VERSION=$PAYMENTS_VERSION
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
