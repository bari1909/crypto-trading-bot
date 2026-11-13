FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY dashboard ./dashboard
RUN useradd --create-home trader && mkdir -p /app/data && chown trader /app/data
USER trader
CMD ["python", "-m", "app.worker"]
