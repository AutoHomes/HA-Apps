# Changelog

## 1.0.1
- Fix: doorbell presses weren't coming through on some locks. Locks are now listened to on both of Tuya's topics for each device, and every data point gets its real name (e.g. `doorbell` instead of `dp_24`).
- Live activity can show **All devices**, to spot a doorbell that's reported by a separate device (such as the camera of a video lock).
- Shows how many messages Tuya has sent, per device and in total, so it's clear whether the live feed is working.
- The Log tab now shows the live-feed connection, what Tuya accepted, each lock's data points, and every raw message from a lock.

## 1.0.0
- Run a Home Assistant script when the doorbell button on a Tuya / Smart Life smart lock is pressed, including locks the official Tuya integration doesn't support.
- Sign in by scanning a QR code with the Smart Life app. No Tuya developer account needed.
- Finds your locks automatically, or watches the ones you list.
- Live activity view of everything the lock reports, to find the right "doorbell code" for your model.
- Cooldown so one press rings once, a "Ring now" test button, and `tuya_doorbell_pressed` / `tuya_lock_report` events for automations.
