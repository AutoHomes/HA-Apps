# The default value here is a safety net for a known 2026 Supervisor build
# quirk where BUILD_FROM can arrive blank; build.yaml still picks the correct
# per-architecture image during a normal install.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

# python3/pip -> to install websockify (the WebSocket<->TCP bridge)
# git         -> to fetch the noVNC web client
RUN apk add --no-cache python3 py3-pip git \
    && pip3 install --break-system-packages --no-cache-dir websockify \
    && git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc \
    && rm -rf /opt/novnc/.git /opt/novnc/tests \
    && cp /opt/novnc/vnc.html /opt/novnc/vnc.html.orig

COPY run.sh /
RUN sed -i 's/\r$//' /run.sh \
    && chmod a+x /run.sh

CMD [ "/run.sh" ]
