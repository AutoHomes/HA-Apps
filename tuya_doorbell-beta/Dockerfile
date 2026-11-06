# The default value here is a safety net for a known 2026 Supervisor build
# quirk where BUILD_FROM can arrive blank; build.yaml still picks the correct
# per-architecture image during a normal install.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

# py3-aiohttp       -> web server + Home Assistant API client
# py3-requests, py3-cryptography, py3-paho-mqtt
#                   -> what Tuya's device-sharing SDK needs, prebuilt by Alpine
#                      (no compiling cryptography on armv7)
# tuya-device-sharing-sdk -> Smart Life sign-in + real-time device messages
#                      (the same SDK Home Assistant's Tuya integration uses);
#                      --no-deps keeps pip from replacing Alpine's packages
# segno             -> pure-Python QR code for the Smart Life sign-in
RUN apk add --no-cache python3 py3-aiohttp py3-requests py3-cryptography py3-paho-mqtt py3-pip tzdata \
    && pip3 install --break-system-packages --no-cache-dir --no-deps \
        tuya-device-sharing-sdk==0.2.15 \
        segno==1.6.6

COPY app /app
COPY run.sh /
RUN sed -i 's/\r$//' /run.sh \
    && chmod a+x /run.sh

CMD [ "/run.sh" ]
