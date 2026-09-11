ARG BUILD_FROM="ghcr.io/home-assistant/amd64-base:3.19"
FROM $BUILD_FROM

ENV LANG C.UTF-8

# Install dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip

# Copy root filesystem
COPY run.sh /
COPY backend /backend/

# Install python requirements
RUN pip3 install --no-cache-dir -r /backend/requirements.txt --break-system-packages

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
