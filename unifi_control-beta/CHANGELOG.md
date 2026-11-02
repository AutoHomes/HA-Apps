# Changelog

## 1.0.1
- Fix: the Configuration tab couldn't save "Durations (hours)" ("Invalid list for option 'grant_durations'"). Durations are now entered like Notify services: type a number, press Enter.
- Clearer error when "Local - verify certificate" is on but the console's certificate is self-signed or doesn't match the host.

## 1.0.0
- Port access: grant an IP access to a port forward through a UniFi IP list, for 1/2/5 hours or with no expiry. Expired access is removed automatically.
- "Open to anyone" for a limited time, closed again automatically.
- WiFi: show a password (with scan-to-join QR code), or set or generate a new one. Admin chooses which networks others see.
- Knows which Home Assistant user is using it. Admins control per-user permissions and which rules and networks are visible.
- Admin phone notifications and a `unifi_control_event` for every action.
- Local (UniFi OS console, Cloud Key, self-hosted) or cloud (Site Manager connector) connection, with optional gateway force-provision.
