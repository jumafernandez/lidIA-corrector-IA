FROM python:3.12-slim

WORKDIR /srv/lidia
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY seed.py .

ENV DATA_DIR=/srv/lidia/data
VOLUME /srv/lidia/data
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
