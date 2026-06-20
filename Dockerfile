FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 80

CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:80", "--workers", "1", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]