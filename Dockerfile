FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    fontconfig \
    fonts-dejavu-core \
    fonts-ocr-a \
    fonts-ocr-b \
    libusb-1.0-0 \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Flask serves "/" from templates/index.html; the repo also keeps a copy at
# the repo root, so make sure the template exists whichever layout is used.
RUN mkdir -p /app/data /app/templates && \
    if [ ! -f /app/templates/index.html ] && [ -f /app/index.html ]; then \
      cp /app/index.html /app/templates/index.html; \
    fi

ENV PORT=8013
ENV SERIAL_DB=/app/data/label_serials.db
ENV PRINTER_MODEL=QL-800
ENV PRINTER_DISPLAY_NAME="Brother QL-800"
# Printer resilience tuning (see PRINTER_TROUBLESHOOTING.md). Defaults match
# app.py and normally do not need changing.
ENV PRINT_MAX_RETRIES=5
ENV PRINT_RETRY_DELAY=2.5
ENV PRINT_WAKE_WAIT=1.5
ENV PRINT_STATUS_TIMEOUT=10
ENV PRINTER_AUTO_FALLBACK=1
# Optional comma-separated fallback URIs, e.g. "file:///dev/usb/lp0".
ENV PRINTER_FALLBACKS=""
EXPOSE 8013

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8013'))"

CMD ["python", "app.py"]
