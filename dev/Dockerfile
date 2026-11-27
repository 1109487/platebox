# Development / test image: the same Python + OpenSSH (Debian 12) the Raspberry Pi runs.
# Not the product: the product is installed on the box by deploy/install.sh.
FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 openssh-server iproute2 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/platebox
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

# Warm the model cache so the first picture is not a download.
ARG PB_DETECTOR=yolo-v9-t-384-license-plate-end2end
ARG PB_OCR=cct-s-v2-global-model
ENV PB_DETECTOR=${PB_DETECTOR} PB_OCR=${PB_OCR}
RUN python -c "from fast_alpr import ALPR; ALPR(detector_model='${PB_DETECTOR}', ocr_model='${PB_OCR}'); print('models cached')"
