# PlateBox on a server (a VPS): the camera sends its pictures here over the internet, plates are read here, and the
# page is opened from anywhere. Built from the plate-box folder:  docker compose -f server/docker-compose.yml build
FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 \
 && rm -rf /var/lib/apt/lists/*

# Not root: the program only needs its own data folder and two ports above 1024.
RUN useradd --system --create-home --home-dir /home/platebox --uid 990 platebox \
 && mkdir -p /var/lib/platebox && chown platebox:platebox /var/lib/platebox

WORKDIR /opt/platebox
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

USER platebox
# The plate models are fetched once, here, so the running server never downloads anything.
ARG PB_DETECTOR=yolo-v9-t-384-license-plate-end2end
ARG PB_OCR=cct-s-v2-global-model
ENV PB_DETECTOR=${PB_DETECTOR} PB_OCR=${PB_OCR}
RUN python -c "from fast_alpr import ALPR; ALPR(detector_model='${PB_DETECTOR}', ocr_model='${PB_OCR}'); print('models cached')"

COPY --chown=platebox:platebox platebox ./platebox

ENV PLATEBOX_SERVER=1 \
    PLATEBOX_DATA=/var/lib/platebox \
    PLATEBOX_SFTP_ROOT=/var/lib/platebox/sftp \
    PLATEBOX_PORT=8080 \
    PLATEBOX_SFTP_PORT=2222 \
    PLATEBOX_COOKIE_SECURE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080 2222
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "-m", "platebox"]
