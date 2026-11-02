# UniFi Control

Everyday UniFi tasks for everyone in the house, from the Home Assistant sidebar, with you (the admin) deciding who can do what:

- **Port access**: let a specific IP through a port forward for 1, 2 or 5 hours (or with no expiry), using a UniFi IP list. Access is removed automatically when it runs out.
- **Open to anyone**: open a port forward to the whole internet, but only for a limited time.
- **WiFi passwords**: show a network's password (with a scan-to-join QR code), set a new one or generate a random one.
- **Per-user permissions**: the app knows which Home Assistant user is using it. No separate login.
- **Admin alerts**: every grant, revoke, expiry, open/close and WiFi change is pushed to your phone.

## 1. Give the app a way into UniFi

### Local (recommended)

Point the app at the device that runs the **UniFi Network application**: a Cloud Gateway / Dream Machine (UCG, UDM, UDR…), a Cloud Key, or a self-hosted Network server. Requests stay on your LAN, and changes reach UniFi straight away.

Pick one way to sign in:

- **Username + password**: on the console, create a **local** admin user (UniFi OS → Admins & Users → "Restrict to local access only"), with the *Site Admin* role (or higher) in Network. UI.com accounts with MFA can't sign in from apps.
- **API key**: UniFi Network → Settings → Control Plane → Integrations → *Create API key*. This works on UniFi OS consoles only, not on a legacy self-hosted Network server.

> **Changes slow to reach your gateway?** If your Network application runs on a Cloud Key and the gateway is a separate device, UniFi pushes ("provisions") config changes to the gateway on its own schedule. Turn on **Force-provision gateway** and the app pushes the change to the gateway right after every edit. This may cause a brief blip on the gateway, so leave it off unless you see delays.

### Cloud

Uses Ubiquiti's Site Manager *connector*, which relays requests through `api.ui.com` to your console. Nothing on your network has to be reachable by Home Assistant, but every request makes a round trip to the cloud.

- Create a **Site Manager API key** at [unifi.ui.com](https://unifi.ui.com) → API.
- The **console ID** is the long ID in the address bar when you open the console on unifi.ui.com.
- The console needs reasonably current firmware (UniFi OS 5.0.3 or newer is what Ubiquiti lists for the connector).

Ubiquiti documents the connector mainly for its official Integration API. This app uses the same API as the UniFi web interface, so whether Ubiquiti relays writes through the connector depends on your console and firmware. After setting it up, check **Admin → UniFi connection** in the app and try one grant. If the cloud route refuses changes, switch to local mode.

## 2. Configuration tab

| Option | What it does |
|---|---|
| `connection_mode` | `local` or `cloud` (see above) |
| `local_host` | IP or hostname of the console, e.g. `192.168.1.1`. `https://…` and `host:port` are accepted too |
| `local_port` | `443` for UniFi OS consoles, usually `8443` for a self-hosted Network server |
| `local_username` / `local_password` | Local UniFi admin account (leave empty when using an API key) |
| `local_api_key` | UniFi Network API key (instead of username/password) |
| `local_verify_ssl` | Leave **off** unless your console has a real certificate. Consoles ship with a self-signed one |
| `cloud_api_key` / `cloud_console_id` | For `cloud` mode |
| `site` | UniFi site's short name. `default` unless you run several sites (it's the `default` in `…/network/default/dashboard` in the UniFi URL) |
| `force_provision` | Push changes to the gateway immediately (see above) |
| `notify_services` | Where admin alerts go: one entry per notify service, e.g. `mobile_app_ashish_phone` (find yours under Developer tools → Actions, search "notify.") |
| `grant_durations` | The time choices, in whole hours (1-720), offered for grants and for "open to anyone". Type each one and press Enter. Default `1, 2, 5` |
| `public_ip_lookup` | Lets the browser ask `api.ipify.org` for its own public IP so "This device" can be filled in for you. Turn off if you'd rather not contact a third party |
| `wifi_password_length` | Length of generated WiFi passwords (8-63) |
| `log_level` | `debug` shows every UniFi call in the Log tab |

Restart the app after changing these.

## 3. First run (as an admin)

On the app's **Info** tab, turn on **Show in sidebar** and start the app. The **UniFi** sidebar entry is then visible to every Home Assistant user, not just admins. What each person can do inside is decided by the app. Open it:

1. **Ports**: your existing port forwards are listed under "Other port forwards in UniFi". Click **Manage with this app** on the ones you want to control:
   - If the rule already uses an IP list (firewall group) as its source, the app adopts that list.
   - Otherwise it creates an IP list called `HA access - <rule name>`, copies in any source IP the rule already had, and limits the rule to that list. **A rule that was open to the world becomes closed** until someone grants access.
   - Turn on **Visible to users** for the rules household members may use.
2. **WiFi**: turn on **Visible to users** for the networks others may see.
3. **Admin**: set the **default permissions** for non-admin users, customise individual people, and press **Send test notification**.

Home Assistant **admins** always have every feature. Everyone else gets the defaults unless you customise them:

| Permission | Allows |
|---|---|
| Grant port access to an IP | Add an IP (or a /24-or-smaller range) to a visible rule for one of the durations |
| Grant port access with no expiry | Also offer "No expiry" |
| Open a port to anyone (time-limited) | Turn a visible rule's IP limit off for one of the durations, and close it again early |
| View WiFi passwords | Show the password and QR code of visible networks |
| Change WiFi passwords | Set or generate a new password for visible networks |

Non-admins only see their own grants. Admins see everyone's, plus the History tab.

## How port access works

- **Granting** adds the IP to the rule's UniFi IP list. **Expiry or revoke** takes it out, unless another active grant still needs the same IP, or the IP was already in the list before the app touched it (those are never removed).
- Granting the same IP again on the same rule replaces your previous expiry instead of adding a duplicate.
- **"This device"** is your device's public IP as the internet sees it, which is what a port forward checks. If you're at home on WiFi, that's your home's own IP. Type the address of the place you'll connect *from* instead.
- **Open to anyone** turns off the rule's source limit for the chosen time, then puts the IP list back. It is always time-limited, admins included.
- An IP list can't be empty in UniFi, so when the last grant ends the app leaves the placeholder `192.0.2.1` in it. That address is reserved for documentation (RFC 5737) and never appears on the internet.
- Expiries are checked every 15 seconds. If Home Assistant or the app was stopped when something was due, it's removed as soon as the app starts again (and the notification says so). If UniFi can't be reached, the app keeps retrying every minute and alerts you once.
- **Stop managing** ends all grants made through the app and closes any open window. The rule stays limited to its IP list in UniFi.
- If someone changes a managed rule in UniFi directly, the app shows **Changed in UniFi** with a **Repair** button.

## Automations

Every action also fires a Home Assistant event, `unifi_control_event`, with `action` (`grant`, `revoke`, `expire`, `open`, `close`, `wifi_password`, `manage`, `unmanage`, `error`), a readable `summary`, and details such as `ip`, `rule`, `expires_at`, `actor`. For example, to flash a light whenever a port is opened to anyone:

```yaml
triggers:
  - trigger: event
    event_type: unifi_control_event
    event_data:
      action: open
actions:
  - action: light.turn_on
    target:
      entity_id: light.office
    data:
      flash: short
```

## Security notes

- The app is only reachable through Home Assistant's authenticated Ingress. It refuses requests from anywhere else, so other apps on the internal network can't pretend to be a user.
- WiFi passwords are only fetched when someone presses **Show password**, and are never included in notifications.
- The UniFi account or API key needs admin rights in UniFi Network. Store it like any other admin credential.
- The app's state (rules it manages, active grants, permissions, history) lives in its own `/data` folder and is included in Home Assistant backups.

## Troubleshooting

- **"UniFi rejected the username/password"**: use a *local* UniFi account. Cloud (UI.com) accounts with MFA don't work for API logins.
- **"UniFi has no site called …"**: set `site` to the short site name from the UniFi URL (`…/network/<this>/dashboard`).
- **A user can't see the UniFi sidebar entry**: check **Show in sidebar** is on (app's Info tab) and that the app is running, then have them reload the page.
- **A user sees "Nothing has been enabled for you"**: give them at least one permission on the **Admin** tab, or enable it in the defaults.
- Set `log_level` to `debug` and check the **Log** tab. Every UniFi request and its result is listed there.
