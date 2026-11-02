# The default value here is a safety net for a known 2026 Supervisor build
# quirk where BUILD_FROM can arrive blank; build.yaml still picks the correct
# per-architecture image during a normal install.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

# py3-aiohttp -> web server + HTTP client (prebuilt by Alpine, no compiling on armv7)
# segno       -> pure-Python QR codes for "scan to join" WiFi
# tzdata      -> notification times in Home Assistant's timezone
RUN apk add --no-cache python3 py3-aiohttp py3-pip tzdata \
    && pip3 install --break-system-packages --no-cache-dir segno==1.6.6

COPY app /app
COPY run.sh /
RUN sed -i 's/\r$//' /run.sh \
    && chmod a+x /run.sh

CMD [ "/run.sh" ]
