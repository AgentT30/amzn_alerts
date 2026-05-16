FROM python:3.13.9-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY amazon_price_check.py .
COPY README.md .
COPY docker ./docker

RUN chmod +x /app/docker/checker.sh
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "amazon_price_check:create_app()"]
