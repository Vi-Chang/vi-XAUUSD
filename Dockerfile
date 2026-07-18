FROM python:3.12-slim

WORKDIR /srv/xauusd

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# 雲端平台(Zeabur/Railway 等)會注入 PORT;本機預設 8000
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
