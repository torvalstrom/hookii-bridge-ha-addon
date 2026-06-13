# Getting Started — the simple, step-by-step version

This is the no-jargon walkthrough for getting your Hookii Neomow mower into
Home Assistant. If you can install a Home Assistant add-on, you can do this.
Set aside about 20 minutes.

> This guide is for **Home Assistant OS** or **Home Assistant Supervised** (the
> versions with an "Add-ons" menu). If your Home Assistant doesn't have an
> Add-ons menu, see "Install path B" in the [README](README.md) instead.

You'll do six things, in order:

1. Set up an MQTT broker (the messaging system HA uses)
2. Create a **separate** Hookii account for the bridge
3. Add this add-on repository to Home Assistant
4. Install and configure the **Hookii Bridge** add-on
5. Check that your mower appeared
6. (Optional) Add the live **Mower Map**

---

## 1. Set up MQTT (Mosquitto)

MQTT is the little messaging system Home Assistant uses to talk to devices. The
bridge sends your mower's data through it. If you already use the Mosquitto
add-on, skip to step 2.

1. **Settings → Add-ons → Add-on Store**
2. Search for **"Mosquitto broker"**, open it, click **Install**, then **Start**.
3. Make a username/password for the bridge to log in with:
   **Settings → People → Users → Add user**. Call it something like `mqtt`,
   give it a password, and write both down. (The Mosquitto add-on accepts any
   Home Assistant user, so this just works.)
4. Home Assistant usually finds Mosquitto automatically under
   **Settings → Devices & Services**. If you see a "MQTT" box offering to
   configure, click **Configure → Submit**.

That's the MQTT part done. You won't touch it again.

---

## 2. Create a SEPARATE Hookii account for the bridge

**This is the step people skip, and it causes the most trouble.** Hookii's
servers only allow **one** active login per account. If the bridge logs in with
the same account as your phone, the two will kick each other out every few
minutes — and you'll keep getting logged out of the Hookii app.

The fix takes 5 minutes:

1. In the Hookii mobile app, **create a second account** with a different email
   (any email you control).
2. Log back into your **main** account, and **share each mower** to the new
   account (in the app: device settings → device sharing / share device).
3. You'll give the bridge the **new** account's email and password later. Your
   phone keeps using your main account. No more logouts.

---

## 3. Add this repository to Home Assistant

1. **Settings → Add-ons → Add-on Store**
2. Click the **⋮** menu (top-right corner) → **Repositories**
3. Paste this address and click **Add**, then **Close**:

   ```
   https://github.com/torvalstrom/hookii-bridge-ha-addon
   ```

4. Reload the page. Two new add-ons appear: **Hookii Bridge** and
   **Hookii Mower Map**.

---

## 4. Install and configure the Hookii Bridge

1. Open **Hookii Bridge** → **Install** (this can take a couple of minutes).
2. Go to the **Configuration** tab. Each field has a short explanation right
   under it — but here are the ones that matter:
   - **Hookii account email / password** → the **second** (bridge) account you
     made in step 2. *Not* your main phone account.
   - **Mower serial number(s)** → the 16-character code that starts with `HKX`.
     It's printed under your mower and shown in the Hookii app under device
     info. More than one mower? Separate them with commas.
   - **Which Hookii cloud** → pick **prod** if your mower runs normal firmware
     (this is almost everyone). Only pick **beta** if you deliberately switched
     your mower to the Beta firmware channel.
   - **MQTT username / password** → the `mqtt` user you created in step 1.
   - Leave everything marked **"(advanced)"** blank.
3. Click **Save**, then go to the **Info** tab and click **Start**.
4. Open the **Log** tab. Within a few seconds you should see lines like:

   ```
   login OK ...
   cloud-mqtt connected ...
   discovery: published 20 entities for HKX...
   ```

   That means it's working.

---

## 5. Check that your mower appeared

Go to **Settings → Devices & Services → MQTT**. Your mower's controls and
sensors (a lawn-mower card, Start/Pause/Dock buttons, battery, etc.) appear
automatically — no manual configuration. Drop them onto a dashboard wherever
you like.

If nothing appears, see **"It's not working"** at the bottom.

---

## 6. The live Mower Map (built in)

The Mower Map draws a live picture of your yard — the boundary, where the mower
has cut, and its current position.

**Since v1.5.0 the map is built into this add-on — there is nothing extra to
install.** It automatically uses the **Mower serials** you set in step 4, so it
just works once the bridge is running.

1. Open the **Mower Map** entry in the left sidebar (Home Assistant adds it
   automatically when the add-on starts).
2. That's it — the grid shows one tile per mower.

To put the map on a dashboard instead of (or as well as) the sidebar: edit a
dashboard → **+ Add Card → Manual**, and paste:

```yaml
type: iframe
url: /hassio/ingress/hookii_bridge/page/garden
aspect_ratio: 100%
```

Replace `garden` with one of your mower nicknames. The bridge derives each
nickname from the serial: it is the serial **lowercased** (e.g. serial
`HKX1EB100JD25010115` → nickname `hkx1eb100jd25010115`). For the all-mowers
grid use `url: /hassio/ingress/hookii_bridge/all`.

The map starts blank and fills in once the mower streams its first update. The
yard **outline** can take a while to appear (the Hookii cloud only sends it
occasionally) — the live position shows up right away.

> **Upgrading from the old separate "Hookii Mower Map" add-on?** You can
> **uninstall** it — its job is now done by the bridge. Any dashboard iframe
> cards that pointed at `/hassio/ingress/hookii_mower_map/...` need their URL
> changed to `/hassio/ingress/hookii_bridge/...` (note: `hookii_bridge`).

---

## It's not working

| What you see | What it usually means | Fix |
|---|---|---|
| Log says `login` failed | Wrong account details, **or** you used your main account | Double-check the email/password; make sure it's the dedicated bridge account from step 2 |
| You keep getting logged out of the **Hookii app** | The bridge is sharing your main account | Use a separate account for the bridge (step 2) |
| Log says it can't connect to MQTT | The broker address/username is wrong | Broker address is `core-mosquitto` for the official add-on; username/password is the `mqtt` user from step 1 |
| Bridge runs but **no entities** in Home Assistant | The MQTT **integration** isn't set up in HA | Settings → Devices & Services → add/Configure **MQTT** against the same broker |
| Mower Map tile stays blank | The bridge hasn't published a position yet | Confirm the bridge Log shows `discovery: published…`; live position appears within seconds, the yard outline can take minutes-to-hours |
| Mower Map panel shows text/JSON, not a picture | You're on the old v1.5.0-beta1 | Update the add-on to **v1.5.0-beta2 or newer**, then reload the panel |

Still stuck? The full reference (including the built-in Mower Map) is in the
[Hookii Bridge docs](hookii_bridge/DOCS.md).
