# Tuya Lock Doorbell

Runs a Home Assistant script when someone presses the doorbell button on a Tuya / Smart Life smart lock. It works with locks that Home Assistant's own Tuya integration doesn't support.

- **Sign in with a QR code** from the Smart Life (or Tuya Smart) app. You don't need a Tuya developer account.
- **Finds your locks** on that account automatically, or watches the ones you list.
- **Runs your script** on every press, and fires a `tuya_doorbell_pressed` event for automations.
- **Live activity** shows everything the lock reports as it happens, so you can see what your model sends when the button is pressed.
- **Listen-only**: the app never sends commands to the lock, so it can't unlock anything.

## How it works

Your lock reports every event (doorbell, unlocks, alarms, battery) to Tuya's cloud, which pushes it to signed-in apps over a live MQTT feed. Home Assistant's Tuya integration uses that same feed, but it only subscribes to devices it created entities for. An unsupported lock never gets subscribed, so its doorbell presses never reach Home Assistant.

This app signs in to Smart Life the same way, subscribes to your locks directly, and reads their raw reports, including data points Tuya doesn't map for Home Assistant. It needs an internet connection, because the lock only talks to Tuya's cloud.

## 1. Create the script to run

Create the script under **Settings → Automations & scenes → Scripts**. It receives these variables:

| Variable | Example |
|---|---|
| `lock_name` | `Front door` |
| `lock_id` | `bf3a…` (Tuya device ID) |
| `code` / `value` | `doorbell` / `true`: what the lock reported |
| `test` | `true` when started with the app's **Ring now (test)** button |

For example, to send a phone notification and flash the hall light:

```yaml
alias: Doorbell pressed
mode: single
sequence:
  - action: notify.mobile_app_my_phone
    data:
      title: "🔔 Doorbell"
      message: "Someone is at the {{ lock_name }}"
  - action: light.turn_on
    target:
      entity_id: light.hall
    data:
      flash: short
```

## 2. Configuration tab

| Option | What it does |
|---|---|
| `script` | The script to run, e.g. `script.doorbell_pressed`. The app starts it with `script.turn_on`, so a long script doesn't hold up the next ring |
| `locks` | Leave empty to watch every lock on your account. Otherwise type each lock's **name** (as shown in Smart Life) or **device ID** and press Enter. The app's page lists every device with its ID |
| `doorbell_codes` | Which reports count as a press. Default `doorbell`, Tuya's standard data point. `code` matches any "on" value (`true`, a number, a non-empty text). `code=value` matches one value only, e.g. `alarm_lock=doorbell` |
| `cooldown_seconds` | After a ring, ignore more presses from the same lock for this many seconds (default `10`). This stops one press, or someone pressing again and again, from running the script several times. `0` turns it off |
| `log_level` | `debug` also logs every raw message from the lock |

Restart the app after changing these.

## 3. Sign in (as an admin)

Start the app and open **Doorbell** in the sidebar. Turn on **Show in sidebar** on the app's Info tab if it isn't there. Only Home Assistant admins can open it.

1. In the Smart Life app, go to **Me → ⚙ Settings → Account and Security → User Code**, and type that code into the app's page.
2. Press **Show QR code**, scan it with the Smart Life app (scan icon at the top right of the Home tab), and confirm.

Use the account the lock is paired to, or one it has been shared with. The QR code is only valid for about 3 minutes. If it runs out, press **New QR code**.

**Already using Home Assistant's Tuya integration?** Signing in here with the same account usually works, because each sign-in gets its own token. Some people have reported Tuya signing the other one out, though. If your Tuya integration asks you to sign in again afterwards, give this app its own Smart Life account. Create a second account, add it to your home in the Smart Life app (**Me → Home Management → your home → Add Member**), and sign in here with it.

The sign-in is saved, so it survives restarts and updates, and the app renews its access token on its own.

## 4. Check it works

The **Locks** card lists the locks the app is watching, with the data points Tuya lists for each. The doorbell code shows highlighted if Tuya lists it.

Press the doorbell button on the lock. Within a few seconds, a line with a 🔔 should appear under **Live activity**, followed by "Ran script.…". Battery locks are asleep until you touch them, so the first report can take 2 to 5 seconds.

### If your lock reports something else

Lock models differ. If nothing gets a 🔔 when you press the button, look at what showed up in **Live activity** at that moment, for example `doorbell_call = true` or `dp_27 = true`. Add that code under `doorbell_codes` on the Configuration tab and restart the app. `dp_<number>` is how the app names a data point Tuya hasn't given a code. It works as a doorbell code like any other.

If the lock reports the same code for other things too (e.g. `alarm_lock`), match the value as well: `alarm_lock=doorbell`.

## Automations

Every press also fires **`tuya_doorbell_pressed`** with `lock_name`, `lock_id`, `code`, `value` and `test`. You can use that instead of, or as well as, the script:

```yaml
triggers:
  - trigger: event
    event_type: tuya_doorbell_pressed
    event_data:
      lock_name: Front door
actions:
  - action: media_player.play_media
    target:
      entity_id: media_player.kitchen
    data:
      media_content_id: media-source://media_source/local/chime.mp3
      media_content_type: music
```

Everything else a watched lock reports fires **`tuya_lock_report`** with `lock_name`, `lock_id`, `code`, `value` and `dp_id`. That gives you, for example, a fingerprint unlock even though Home Assistant doesn't support the lock:

```yaml
triggers:
  - trigger: event
    event_type: tuya_lock_report
    event_data:
      code: unlock_fingerprint
actions:
  - action: notify.mobile_app_my_phone
    data:
      message: "Front door unlocked by fingerprint #{{ trigger.event.data.value }}"
```

## Security notes

- The app is only reachable through Home Assistant's authenticated Ingress, and only by admins. It refuses requests from anywhere else.
- It only listens. It never sends commands to your lock or any other device.
- The Smart Life sign-in token is stored in the app's own `/data` folder and is included in Home Assistant backups. **Sign out** revokes it at Tuya. You can also remove it in the Smart Life app.
- The app signs in to Tuya the same way Home Assistant's own Tuya integration does.

## Troubleshooting

- **"Smart Life didn't accept that user code"**: use the *User Code* from **Me → Settings → Account and Security**, not your email or password.
- **No locks found**: the **Other Smart Life devices** list on the app's page shows everything on the account. Add your lock's name or ID under `locks` and restart the app. If your lock isn't listed at all, it's paired to a different account. Share it to this one in the Smart Life app, or sign in with the owner's account.
- **Unlocks show in Live activity but the doorbell press shows nothing at all**: the lock doesn't report the press to Tuya's device feed (some models only ring their own chime), so the app can't see it.
- **"Can't reach Tuya"**: the app retries on its own. If it doesn't recover, for example after you removed the authorization in the Smart Life app, sign out and sign in again.
- **Added a lock after starting the app**: press **Refresh devices**.
- Set `log_level` to `debug` and check the **Log** tab to see every raw message from your locks.
