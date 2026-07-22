# VNC Viewer

A browser-based VNC client (noVNC) that lives in your Home Assistant sidebar. Add one device or many — each one connects automatically, no login form, no clicking Connect.

## Configuration

Go to the **Configuration** tab and use **Add** under "Devices" for every VNC target you want. Each device has:

| Field | What it does |
|---|---|
| `name` | Label shown on its card in the picker |
| `host` | IP address or hostname of that device |
| `port` | TCP port of its VNC server, usually `5900` |
| `password` | That server's VNC password. Leave as-is if it has none |
| `view_only` | On = you can only watch that device, not control it |

`resize` (top level, shared by all devices) — `scale` (recommended) fits the picture to your window; `remote` asks the server to change its own resolution to match; `off` disables resizing.

Restart the app after changing any of this.

## How it behaves

- **One device configured** → opening the app connects you straight in, same as before.
- **Two or more** → opening the app shows a small picker grid; click a card and it connects immediately. A "Devices" link in the corner of the viewer takes you back to the picker.
- **None yet** → the app tells you to add one on the Configuration tab.

Under the hood this all runs through a single `websockify` process using its built-in token-based routing, so adding more devices doesn't cost you another app install or another port.

## Clipboard

Open the thin arrow tab on the left edge of the viewer — it slides out a panel with a Clipboard text box. Paste into it to send text to the remote machine; text copied on the remote side shows up there for you to copy out. That's the standard noVNC clipboard flow.

Fully automatic, no-click OS clipboard sync isn't something stock noVNC does — browsers deliberately sandbox clipboard access. If seamless bidirectional sync matters a lot to you, the practical upgrade path is swapping the server side for **KasmVNC**, which handles it properly.

## Security note

Each device's password lives in this app's configuration and is sent once inside that device's auto-connect URL so its card can "just work." The app is only reachable through Home Assistant's own authenticated Ingress, not exposed on the network by itself — but treat those passwords with the same care as your Home Assistant login.
