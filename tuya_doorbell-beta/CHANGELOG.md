# Changelog

## 1.0.0
- Run a Home Assistant script when the doorbell button on a Tuya / Smart Life smart lock is pressed, including locks the official Tuya integration doesn't support.
- Sign in by scanning a QR code with the Smart Life app. No Tuya developer account needed.
- Finds your locks automatically, or watches the ones you list.
- Live activity view of everything the lock reports, to find the right "doorbell code" for your model.
- Cooldown so one press rings once, a "Ring now" test button, and `tuya_doorbell_pressed` / `tuya_lock_report` events for automations.
