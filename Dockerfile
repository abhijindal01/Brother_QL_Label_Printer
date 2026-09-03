FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    fontconfig \
    fonts-dejavu-core \
    fonts-ocr-a \
    fonts-ocr-b \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data

ENV PORT=8013
ENV SERIAL_DB=/app/data/label_serials.db
EXPOSE 8013

CMD ["python", "app.py"]
