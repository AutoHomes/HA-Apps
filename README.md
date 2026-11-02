# AutoHomes - Home Assistant Apps

This is a Home Assistant **app repository** (apps were called "add-ons" before the Feb 2026 rename — same thing, new name).

## Installation

[![Add repository on my Home Assistant][repository-badge]][repository-url]

Or add this manually as guided by [Home Assistant website](https://home-assistant.io/hassio/installing_third_party_addons):
In Home Assistant, go to **Settings → Apps → Apps store**, click the **⋮** menu (top right) → **Repositories**, and paste this repo's GitHub URL.

## Apps in this repository

### [VNC Viewer](./novnc_viewer)
A browser-based VNC client (noVNC) that sits in your Home Assistant sidebar. Add one device or a whole list of them in its Configuration tab — one device connects straight in, several show a quick picker grid — and every connection is automatic, no login screen, no clicking Connect.

### [UniFi Control](./unifi_control)
Everyday UniFi tasks for the whole household, with the admin deciding who can do what. Grant an IP timed access to a port forward (through a UniFi IP list that empties itself on expiry), open a port to anyone for a limited time, and show, change or generate WiFi passwords with a scan-to-join QR code. It knows which Home Assistant user is signed in, sends every change to the admin's phone, and talks to your UniFi console locally or through the UniFi cloud.


[repository-badge]: https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg
[repository-url]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FAutoHomes%2FHA-Apps
