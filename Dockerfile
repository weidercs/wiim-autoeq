FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 5173

ENV PYTHONUNBUFFERED=1

CMD ["python3", "src/wiim_autoeq_web.py", "--host", "0.0.0.0", "--port", "5173"]
