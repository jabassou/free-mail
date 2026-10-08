# free-mail

CLI to audit, clean and filter a Free (Zimbra) mailbox.
- **report / clean / folders**: IMAP (`imap.free.fr:993`)
- **filters**: Zimbra SOAP/JSON API (`https://zimbra.free.fr/service/soap`). Free does not document it, so run `filters test` first.

Password is stored in the macOS Keychain (`keyring`, service `freemail`), never on disk.
`FREEMAIL_PASSWORD` env var is supported as a fallback (CI / one-shot).

## Setup

```bash
cd ~/Workspace/Projets/free-mail
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # set user
./freemail.py auth                    # prompts password -> Keychain, tests IMAP login
./freemail.py folders
```

## Workflow

```bash
# 1. Read-only audit
./freemail.py report                  # -> out/report-<ts>/summary.md, senders.csv, domains.csv, biggest.csv, rules.suggested.yaml
./freemail.py report --since 365 --folder INBOX

# 2. Cleanup (dry-run by default)
cp rules.example.yaml rules.yaml      # or start from out/report-*/rules.suggested.yaml
./freemail.py clean                   # shows counts + samples per rule
./freemail.py clean --only newsletters-old --apply   # confirm prompt, CSV log in out/

# 3. Server-side filters
./freemail.py filters test            # validates SOAP auth on Free
./freemail.py filters pull            # backup current filters -> backups/*.json
cp filters.example.yaml filters.yaml
./freemail.py filters plan            # diff: + add, ~ update, = same, keep, - delete (with --replace)
./freemail.py filters push --apply    # auto-backup before write
./freemail.py filters restore -f backups/filters-before-push-<ts>.json
```

## Safety

- `clean` never expunges outside moved messages: `trash` moves to the Trash folder.
- Each `--apply` writes a CSV log (rule, folder, uid, from, subject, action).
- `filters push` keeps server filters not declared in YAML unless `--replace`.
- Server-side IMAP criteria (from/subject/...) are ASCII-only; use `subject_regex` / `from_regex` for accents.

## Notes

- If `filters test` fails (auth refused / non-JSON), Free blocks the SOAP API for this account: manage filters in the webmail instead (Preferences > Filters).
- Zimbra folder paths in filters: create one filter in the webmail, run `filters pull` and mirror the `folderPath` format if `fileinto` misbehaves.

## Web UI (local)

```bash
source .venv/bin/activate
./webui.py                 # opens http://127.0.0.1:8765/?t=<random token>
```

- Bound to 127.0.0.1 only, random per-run token, Host header check. The Zimbra cookie lives in memory only.
- Login: paste the full `Cookie` header from a webmail `SearchRequest` (DevTools > Network > filter `soap`). IMAP uses the Keychain password (`./freemail.py auth`).
- **Dashboard**: live folder counters + quota, charts from the latest `report` (`out/report-*/stats.json`), one-click report refresh.
- **Maintenance**: newsletters > 60d, deliveries > 90d, old notifications, replay server filters on INBOX, empty Trash / Junk (type SUPPRIMER), prune empty folders (filter/rule targets protected). Always simulate first; apply acts on the simulated UIDs only.
- **Filters**: list / toggle / reorder / edit / delete / replay, shadowing badges, templates (Free spam score `X-ProXaD-SC`, `X-Spam-Flag`, block sender, newsletters, social). New/edited filters are checked for name clash, shadowing, interception, duplicates, missing folder (auto-created), short values and discard, with an IMAP preview. Every change backs up the full list to `backups/`.

## Automatic webmail login (no cookie copy-paste)

`webmail_login()` opens a Free webmail session the way a browser does, then reuses its cookies for the Zimbra SOAP API:

1. plain HTTP: fetch the login page, fill the form (hidden fields kept), submit, collect `ZM_AUTH_TOKEN`;
2. fallback: headless browser via Playwright (installed Google Chrome, else Playwright Chromium) for JS-built pages.

Password = Keychain one (`./freemail.py auth`) unless typed in the UI. Tries `user`, then its local part (or set `webmail_user` in `config.yaml`; `login_method: auto|http|browser`).

```bash
pip install playwright            # optional, only for the browser fallback
./freemail.py login               # test; --method browser --show to watch the browser
./freemail.py filters test        # filters commands now log in by themselves
```

The web UI login screen uses it; in automatic mode an expired session is reopened transparently. The manual cookie method stays available as a fallback (captcha, 2FA, page changes).

## Webmail (tab "Boîte mail")

`mailweb.py` turns the web UI into a full mail client, replacing Zimbra's webmail:
- folder tree with unread counts, drag & drop messages onto folders
- message list: newest first (IMAP SORT), 50 per page with infinite scroll, search (sender/subject/recipient, accents via TEXT), filters (unread, flagged, attachments), multi-select bulk actions
- reader: HTML rendered in a sandboxed iframe (no scripts, strict CSP), remote images blocked until "Afficher les images", inline `cid:` images, attachments download, one-click unsubscribe, "Filtrer" opens the filter wizard
- actions: reply / reply all / forward, star, unread, move, delete (Trash; permanent when already in Trash)
- compose: To/Cc/Bcc chips with autocomplete, attachments (20 MB), sent via `smtp.free.fr:465` (SSL) and copied to Sent
- rich-text editor: bold/italic/underline/strike, color, heading, lists, quote, links (Cmd+K), clear formatting; pasted HTML is sanitized, pasted/dropped files become attachments; mail sent as HTML + plain-text alternative
- server drafts: autosave to the IMAP Drafts folder 4 s after the last change (and on minimize/close); each save APPENDs a new version and expunges the previous one; opening a mail in Drafts reopens it in the editor (recipients incl. Bcc, attachments, reply link via `X-FM-Reply`); sending deletes the draft
- forward keeps the original attachments: they are copied server-side (`include` parts), no re-upload from the browser
- conversations: list groups messages by `Message-ID` / `In-Reply-To` / `References` (toggle icon next to refresh); the reader shows the whole thread across the folder, Inbox and Sent (`/api/mail/thread`), last and unread messages expanded
- shortcuts: J/K navigate, C compose, R/A/F reply/all/forward, S star, U unread, V move, # delete, / search, Cmd+Enter send
- IMAP and SMTP now verify TLS certificates (`FREEMAIL_INSECURE_TLS=1` only for local tests)
Config overrides: `smtp_host`, `smtp_port` in `config.yaml`.

## Android (Termux)

The whole app runs on the phone: `webui.py` serves the UI on `127.0.0.1:8765`, Chrome installs it as a PWA, and a background watcher raises Android notifications (Termux:API) for new unread mail in every folder except Sent/Drafts/Trash/Spam.

Phone prerequisites (all from F-Droid, same source for every app): Termux, Termux:API, Termux:Boot, Termux:Widget (optional). Open Termux:Boot once, set Termux battery usage to "Unrestricted", allow Termux:API notifications.

1. On the Mac: `./termux/bundle.sh` -> `free-mail-termux.zip`; copy it to the phone's Download folder (USB, Drive or `adb push free-mail-termux.zip /sdcard/Download/`).
2. In Termux:
   ```bash
   termux-setup-storage
   unzip -o ~/storage/downloads/free-mail-termux.zip -d ~/free-mail
   bash ~/free-mail/termux/install.sh
   ```
3. Chrome opens the app: menu -> "Install app" / "Add to Home screen".

What `install.sh` does: installs python/termux-api/curl + pyyaml/requests, stores the Free password in `~/.config/freemail/password` (chmod 600, Termux-private), tests IMAP, registers `~/.termux/boot/freemail` (auto-start + wake lock) and the `FreeMail` widget, sends a test notification, starts the server.

Server preset: `python webui.py --termux` = `--no-browser --imap-session --notify termux --token-file ~/.config/freemail/token` (`--notify-interval 60` by default, min 20 s). Scripts: `termux/start.sh`, `stop.sh`, `open.sh`; log in `~/.config/freemail/webui.log`.

- Mailbox-only session (IMAP password, no Zimbra): webmail, dashboard and maintenance work; server filters need the webmail session (Playwright/Chrome), so manage them from the Mac.
- Notifications: sender + subject + snippet, tap opens the message (`/#m=<folder>&u=<uid>`), buttons "Marquer lu" / "Ouvrir". They are skipped while the app is on screen (the page reports visibility on each poll).
- The token is persisted (stable home-screen app) and kept in the PWA's localStorage; any other Android app can reach 127.0.0.1 but not the API without it.
- Password file fallback also works on desktop: `FREEMAIL_PASSWORD` > keyring > `$FREEMAIL_PASSWORD_FILE` or `~/.config/freemail/password`.

## Android app (APK)

Native Android app (`android/`, Kotlin + Chaquopy): the same Python backend runs inside the app on 127.0.0.1 (random port, per-install token) and the web UI is shown in a WebView. No Termux needed.

Build the signed APK on the Mac (first run downloads the Android SDK + Gradle, ~10 min; then ~1 min):

```bash
cd android && ./build-apk.sh            # -> ../FreeMail.apk
./build-apk.sh --install                # + adb install on a phone with USB/Wi-Fi debugging
```

Requirements: JDK 17+ (auto: Android Studio's JBR or `brew install --cask temurin@21`), python3.10-3.13 for Chaquopy's build step (auto: `brew install python@3.12`). The script syncs `freemail.py`, `mailweb.py`, `webui.py`, `extras.py` and `web/` into the app before compiling.

Signing: `android/signing/freemail-release.jks` + `keystore.properties` (RSA 4096, 30 years, v2+v3 signatures). Back them up outside the repo with `android/signing-backup.sh` (encrypted archive in `~/Documents`; `--github` also loads them into the repository secrets used by CI): an update must be signed with the same key, otherwise Android refuses it and the app has to be uninstalled (local data lost). `signing/` is gitignored.

On the phone: open `FreeMail.apk` (Files app or Drive), allow "install unknown apps" for that app, install. First launch: Free address + password (checked over IMAP, stored encrypted with an Android Keystore AES-GCM key), then allow notifications and "unrestricted battery".

How it works:
- `MailService` (foreground service, `specialUse`) keeps the process alive; `webui.Notifier` checks the mailbox every 60 s and posts notifications through `Bridge.notifyMail` (sender, folder, subject, snippet; tap opens the mail, "Marquer lu" action). An inexact `AlarmManager` alarm wakes the check every ~5 min (~10 min in Doze).
- Filters (Zimbra): Filtres tab -> "Connexion au webmail Free" opens the webmail in a WebView, fills the stored credentials, captures the `ZM_AUTH_TOKEN` session and hands it to the backend.
- Attachments are saved to Downloads (MediaStore); external links open in the browser; `mailto:` links open the composer; Android back button closes the reader / composer.
- Starts at boot (`BootReceiver`); data (reports, backups) lives in the app's private files dir; app backup is disabled.


## Settings and mail features (v1.4)

Server side in `extras.py` (shared by the desktop UI, Termux and the Android app); state is kept in JSON files next to `config.yaml` (`settings.json`, `outbox.json`, `snooze.json`, gitignored).

- **Settings tab**: display name and HTML signature (sanitized, added to new messages, replies and forwards), undo-send delay (0-30 s), quiet hours, per-folder notifications, "instant" folders (IMAP IDLE push, max 5 connections), language, app lock (Android).
- **Undo send / send later**: the mail goes to the local outbox (`/api/mail/schedule`) and `extras.Scheduler` sends it when due (wakes exactly at the next due item). "Undo" cancels it and reopens the composer; draft attachments are copied into the outbox before the draft is deleted. Failed sends retry with backoff (8 attempts). Scheduled mails are listed in Settings (send now / cancel back to Drafts).
- **Snooze**: moves the mail to the `En attente` folder and records its `Message-ID`; at the chosen time it comes back unread to its folder (and notifies).
- **Search in bodies**: "Body" toggle next to the list filters (`body=1`, IMAP `BODY` criterion, slower).
- **Contacts autocomplete**: recipients of the last 800 sent mails and senders of the last 800 inbox mails, ranked, cached 1 h (`/api/mail/contacts`).
- **Phone gestures**: swipe a row right = archive (or read/unread when there is no Archive folder), left = trash, with 5 s "Undo"; pull down to refresh; the compose toolbar scrolls on one line.
- **Offline mode**: the service worker keeps the last lists, messages and conversations read and serves them when the mailbox is unreachable (banner "Offline").
- **Notifications (Android)**: quiet hours use a silent channel; inline "Reply" from the notification (RemoteInput -> `extras.quick_reply`, threaded reply with signature).
- **App lock (Android)**: fingerprint / face / phone PIN when the app is opened (30 s grace period); the recent-apps preview is hidden while it is on.
- **Updates**: the app checks the latest GitHub release of `jabassou/free-mail` (fixed in `extras.UPDATE_REPO`) every 6 h in the background (ETag, cached in `update.json`), posts a notification once per new version (Android: **Update** button starts the download and the system installer) and shows an update dialog at startup (Update now / Later / Skip this version). "Check for updates" is also in the menu. Releases must be signed with the same key and the repository must stay public (unauthenticated GitHub API).
- **Diagnostics**: own screen in the menu (version, IMAP/webmail/notifier state, Android delivery status, technical log) with Copy / Share.

## Tests

```bash
sudo tests/dovecot.sh start                       # throwaway IMAPS server on 127.0.0.1:1993 (Linux, apt install dovecot-imapd)
pip install -r requirements.txt -r tests/requirements.txt
python -m playwright install chromium
python -m pytest -q
```

- `tests/test_extras.py`: settings sanitizing, quiet hours, outbox, version compare, GitHub release parsing (no network).
- `tests/test_api.py`: the HTTP API against real IMAPS (Dovecot) and SMTPS (aiosmtpd) servers: body search, scheduled send and undo, attachments kept from drafts, snooze round trip, contacts, IDLE push on several folders, muted folders, diagnostics.
- `tests/test_ui.py`: headless Chromium smoke tests (settings, signature, undo toast, phone layout, no French left in the English UI).
- `tests/test_static.py`: Python compiles, inline JS and service worker parse (`node --check`), every API route called by the UI exists.

## CI and releases (GitHub Actions)

Workflows live in `.github/workflows/`.

```bash
mkdir -p .github/workflows && cp ci/*.yml .github/workflows/
```

- `tests.yml`: every push / PR, Ubuntu + Dovecot + Python 3.12 + Playwright.
- `apk.yml`: on a `v*` tag (or manually), builds the signed APK with `android/build-apk.sh` and publishes `FreeMail-vX.Y.Z.apk` as a GitHub release. Needs the secrets `ANDROID_KEYSTORE_B64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD` (`android/signing-backup.sh --github` sets them with the gh CLI).

```bash
echo 1.4.1 > VERSION && git commit -am "Release 1.4.1" && git tag v1.4.1 && git push --follow-tags
```

## License

MIT, see [LICENSE](LICENSE).
