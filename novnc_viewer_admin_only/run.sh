#!/usr/bin/with-contenv bashio

bashio::log.info "novnc_viewer: run.sh starting"

OPTIONS_FILE="/data/options.json"
TOKEN_FILE="/tmp/tokens.cfg"
CARD_FRAGMENT="/tmp/cards.html"
> "${TOKEN_FILE}"
> "${CARD_FRAGMENT}"

# Always start from the untouched original so a plain restart (same container,
# same writable layer) can't append the "Devices" back-link a second time.
if [ -f /opt/novnc/vnc.html.orig ]; then
    cp /opt/novnc/vnc.html.orig /opt/novnc/vnc.html
else
    bashio::log.warning "vnc.html.orig missing - this image may be older than the current Dockerfile. Rebuild (not just restart) the app to pick up the latest changes."
fi

bashio::log.info "Reading options from ${OPTIONS_FILE}"

if [ ! -f "${OPTIONS_FILE}" ]; then
    bashio::log.error "${OPTIONS_FILE} does not exist - starting with no devices configured"
    RESIZE="scale"
    echo '[]' > /tmp/vnc_hosts.json
else
    # Read straight from the options file with jq instead of going through
    # bashio::config, so we're not depending on exactly how it handles a
    # missing key or a default-value argument.
    RESIZE=$(jq -r '.resize // "scale"' "${OPTIONS_FILE}" 2>/dev/null)
    [ -z "${RESIZE}" ] && RESIZE="scale"
    jq -c '.vnc_hosts // []' "${OPTIONS_FILE}" > /tmp/vnc_hosts.json 2>/dev/null
fi

# Belt and braces: whatever ended up in the file, make sure it's valid JSON
# before we start parsing it - fall back to an empty list rather than crash.
jq empty /tmp/vnc_hosts.json 2>/dev/null || echo '[]' > /tmp/vnc_hosts.json
COUNT=$(jq 'length' /tmp/vnc_hosts.json)
bashio::log.info "Configured VNC device(s): ${COUNT}"

USED_SLUGS=" "
INDEX=0
FIRST_URL=""

while IFS= read -r entry; do
    NAME=$(echo "${entry}" | jq -r '.name')
    HOST=$(echo "${entry}" | jq -r '.host')
    PORT=$(echo "${entry}" | jq -r '.port')
    PASSWORD=$(echo "${entry}" | jq -r '.password')
    VIEW_ONLY=$(echo "${entry}" | jq -r '.view_only')

    # Turn the device name into a URL-safe token, de-duping if two devices share a name
    SLUG=$(printf '%s' "${NAME}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+|-+$//g')
    [ -z "${SLUG}" ] && SLUG="device-${INDEX}"
    while echo "${USED_SLUGS}" | grep -qw "${SLUG}"; do
        SLUG="${SLUG}-${INDEX}"
    done
    USED_SLUGS="${USED_SLUGS} ${SLUG} "

    # websockify's token file: "token: host:port" (space after the colon matters)
    echo "${SLUG}: ${HOST}:${PORT}" >> "${TOKEN_FILE}"

    ENC_PASSWORD=$(printf '%s' "${PASSWORD}" | jq -sRr @uri)
    VIEW_PARAM=""
    [ "${VIEW_ONLY}" = "true" ] && VIEW_PARAM="&view_only=true"

    URL="vnc.html?autoconnect=true&resize=${RESIZE}&password=${ENC_PASSWORD}&path=websockify?token=${SLUG}${VIEW_PARAM}"
    [ -z "${FIRST_URL}" ] && FIRST_URL="${URL}"

    ESC_NAME=$(printf '%s' "${NAME}" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
    printf '<a class="card" href="%s"><span class="dot">&#128421;</span>%s</a>\n' "${URL}" "${ESC_NAME}" >> "${CARD_FRAGMENT}"

    INDEX=$((INDEX+1))
done < <(jq -c '.[]' /tmp/vnc_hosts.json)

if [ "${COUNT}" -eq 1 ]; then
    bashio::log.info "Only one device configured - opening it directly"
    cat > /opt/novnc/index.html << EOF
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>VNC Viewer</title></head>
<body style="background:#111318;margin:0">
<script>window.location.replace("${FIRST_URL}");</script>
</body></html>
EOF

elif [ "${COUNT}" -eq 0 ]; then
    bashio::log.warning "No VNC devices configured yet - add one in the Configuration tab"
    cat > /opt/novnc/index.html << 'EOF'
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>VNC Viewer</title></head>
<body style="background:#111318;color:#e3e3e3;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center">
<div>No VNC devices configured yet.<br>Add one on this app's Configuration tab, then restart.</div>
</body></html>
EOF

else
    bashio::log.info "${COUNT} devices configured - showing the picker"
    {
        echo '<!DOCTYPE html><html><head><meta charset="utf-8">'
        echo '<meta name="viewport" content="width=device-width, initial-scale=1">'
        echo '<title>VNC Viewer</title><style>'
        cat << 'CSS'
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#111318;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#e3e3e3}
.wrap{width:100%;max-width:720px;padding:32px 20px}
h1{font-size:1.3rem;font-weight:600;text-align:center;margin:0 0 24px;color:#f3f3f3}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px}
.card{display:flex;flex-direction:column;align-items:center;gap:10px;padding:20px 12px;border-radius:14px;background:#1b1e26;border:1px solid #2a2e38;color:#e3e3e3;text-decoration:none;text-align:center;font-size:.95rem;transition:transform .12s ease,border-color .12s ease,background .12s ease}
.card:hover{transform:translateY(-2px);border-color:#4a90e2;background:#202531}
.dot{width:34px;height:34px;border-radius:8px;background:#4a90e2;display:flex;align-items:center;justify-content:center;font-size:16px}
CSS
        echo '</style></head><body><div class="wrap"><h1>Choose a device</h1><div class="grid">'
        cat "${CARD_FRAGMENT}"
        echo '</div></div></body></html>'
    } > /opt/novnc/index.html

    # Small "back to devices" link on the viewer itself. Appending after </html>
    # is deliberate - browsers still parse and run trailing markup like this,
    # and if a future noVNC update ever changes that, this just quietly has no
    # effect rather than breaking anything.
    cat >> /opt/novnc/vnc.html << 'JSEOF'
<script>
(function(){
  var a = document.createElement('a');
  a.href = 'index.html';
  a.textContent = 'Devices';
  a.style.cssText = 'position:fixed;top:8px;left:8px;z-index:99999;background:#1b1e26;color:#e3e3e3;padding:6px 10px;border-radius:8px;font:12px sans-serif;text-decoration:none;border:1px solid #2a2e38';
  document.body.appendChild(a);
})();
</script>
JSEOF
fi

bashio::log.info "Starting websockify on port 6080 (token-based multi-target mode)"

websockify --web=/opt/novnc --token-plugin TokenFile --token-source "${TOKEN_FILE}" 6080 &
WS_PID=$!
trap 'bashio::log.info "Stopping websockify..."; kill -TERM "${WS_PID}" 2>/dev/null; wait "${WS_PID}" 2>/dev/null' TERM INT

wait "${WS_PID}"
EXIT_CODE=$?

if [ "${EXIT_CODE}" -ne 0 ]; then
    bashio::log.error "websockify exited with code ${EXIT_CODE} - see any output directly above this line for the real cause"
    # Pause instead of letting s6 relaunch us instantly in a tight loop, so
    # the error above actually stays on screen long enough to read.
    sleep 30
fi

exit "${EXIT_CODE}"
