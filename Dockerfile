ARG BUILD_FROM="ghcr.io/home-assistant/amd64-base-debian:bookworm"
FROM $BUILD_FROM

ENV LANG="C.UTF-8"

# Install dependencies
# Pre-install numpy and scipy via apt (fast precompiled deb packages, avoids compiling on ARM/x86)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-numpy \
    python3-scipy \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Cache layer: copy requirements first so Docker caches installed python packages
COPY backend/requirements.txt /tmp/requirements.txt

# Install python requirements from standard PyPI (cached between addon code updates)
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt --break-system-packages

# Copy root filesystem & application code
COPY run.sh /
COPY backend /backend/

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]

