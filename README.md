<div align="center">

<img src="web/icons/icon-192.png" width="96" alt="Free Mail icon">

# Free Mail

**A modern mail app for your Free (`@free.fr`) mailbox.**
Read, write and tidy up your mail in a clean, fast app with real notifications.

[![Latest release](https://img.shields.io/github/v/release/jabassou/free-mail?label=latest%20version&style=for-the-badge)](https://github.com/jabassou/free-mail/releases/latest)
&nbsp;
[![Download the APK](https://img.shields.io/badge/Download-Android%20APK-3DDC84?style=for-the-badge&logo=android&logoColor=white)](https://github.com/jabassou/free-mail/releases/latest)

<img src="docs/images/desktop.png" alt="Free Mail on a computer: folders, message list and an open email" width="900">

</div>

---

## What it does

<table>
<tr>
<td align="center" width="25%"><img src="docs/images/phone-inbox.png" alt="Inbox" width="200"><br><b>Your inbox</b><br>Search, filters (unread, starred, attachments), swipe to archive or delete.</td>
<td align="center" width="25%"><img src="docs/images/phone-reader.png" alt="Reading an email" width="200"><br><b>Read</b><br>Clean display, attachments, remote images blocked until you allow them.</td>
<td align="center" width="25%"><img src="docs/images/phone-compose.png" alt="Writing an email" width="200"><br><b>Write</b><br>Rich text, attachments, signature, send later, undo send.</td>
<td align="center" width="25%"><img src="docs/images/phone-menu.png" alt="Folders menu" width="200"><br><b>All your folders</b><br>Unread counters for every folder and sub-folder.</td>
</tr>
<tr>
<td align="center"><img src="docs/images/phone-dashboard.png" alt="Dashboard" width="200"><br><b>Dashboard</b><br>Storage used, biggest folders, who fills your inbox.</td>
<td align="center"><img src="docs/images/phone-cleanup.png" alt="Maintenance" width="200"><br><b>One-tap cleanup</b><br>Old newsletters, parcel tracking, notifications. Always previewed first.</td>
<td align="center"><img src="docs/images/phone-settings.png" alt="Settings" width="200"><br><b>Your settings</b><br>Signature, quiet hours, per-folder notifications.</td>
<td align="center"><img src="docs/images/phone-inbox-dark.png" alt="Dark theme" width="200"><br><b>Dark theme</b><br>Follows your phone, or choose it yourself.</td>
</tr>
</table>

**Also included:** new-mail notifications with *Mark as read* and *Reply* buttons · snooze a mail until later · one-click unsubscribe · conversations view · fingerprint / face lock · English and French · works offline with the last mails you read.

**Private by design:** the app talks directly to Free's servers. Your password is stored encrypted on your phone and never sent anywhere else. No account, no ads, no tracking.

---

## Install on Android

> Requires **Android 10 or newer** and a **Free mailbox** (`…@free.fr`).
> The app is not on the Play Store: you install it from this page, like any downloaded app.

<table>
<tr>
<td width="70%" valign="top">

**1. Download the app**

On your phone, open the **[latest release](https://github.com/jabassou/free-mail/releases/latest)** (or scan the QR code). Scroll down to **Assets** and tap **`FreeMail-vX.Y.Z.apk`**.

**2. Open the downloaded file**

When the download finishes, tap **Open** (or find the file in *Files → Downloads*).

**3. Allow the installation**

The first time, Android asks to allow apps from this source: tap **Settings**, turn on **Allow from this source**, then go back.
If *Play Protect* shows a warning about an unknown app, tap **More details → Install anyway**.

**4. Install**

Tap **Install**, then **Open**.

</td>
<td width="30%" align="center" valign="top">

<img src="docs/images/qr-latest-release.png" alt="QR code to the latest release" width="180"><br>
<sub>Scan to open the download page</sub>

</td>
</tr>
</table>

### First launch

1. Enter your **Free email address** and its **password**. The app checks them with Free and stores them encrypted on your phone.
2. Allow **notifications**, so you know when new mail arrives.
3. Allow **unrestricted battery** use when asked. Without it, Android may put the app to sleep and notifications arrive late.

That's it: your inbox appears.

---

## Updates

The app checks for new versions by itself (when it starts, then every 30 minutes). When one is out, you get a notification and this window:

<p align="center"><img src="docs/images/phone-update.png" alt="New version available window" width="220"></p>

Tap **Update**, then **Install**. Your mail, settings and password are kept.
You can also check at any time: menu → **Check for updates**.

---

## Questions

<details>
<summary><b>Is it safe to install an app from outside the Play Store?</b></summary>

Every release is built automatically from the public source code on this page and signed with the same key. Android refuses an update signed with another key, so nobody can slip a modified version in place of this one.
</details>

<details>
<summary><b>Notifications arrive late or not at all</b></summary>

Check that notifications are allowed and that battery use is set to **Unrestricted** (Android settings → Apps → Free Mail → Battery). The app's **Diagnostics** screen (menu) shows both.
</details>

<details>
<summary><b>Something does not work</b></summary>

Open menu → **Diagnostics** → **Copy**, then [open an issue](https://github.com/jabassou/free-mail/issues/new) and paste it. It never contains your password, but it can mention mail subjects or addresses: remove anything private before posting.
</details>

<details>
<summary><b>Does it work on a computer?</b></summary>

Yes, the same interface runs on a computer (macOS or Linux) with Python. See the [technical guide](docs/TECHNICAL.md).
</details>

---

<sub>Free Mail is an independent project, not affiliated with Free / Iliad. · [Technical guide](docs/TECHNICAL.md) · [Security](SECURITY.md) · [MIT License](LICENSE)</sub>
