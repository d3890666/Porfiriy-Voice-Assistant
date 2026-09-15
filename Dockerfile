ARG BUILD_FROM="ghcr.io/home-assistant/amd64-base-debian:bookworm"
FROM $BUILD_FROM

ENV LANG="C.UTF-8"

# Install dependencies (libgomp1 is required by onnxruntime / OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy root filesystem
COPY run.sh /
COPY backend /backend/

# Install python requirements
RUN pip3 install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r /backend/requirements.txt --break-system-packages

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

