# Security policy

## Supported versions

Only the latest release (see [Releases](../../releases)) receives fixes.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private reporting:
**Security > Report a vulnerability** on this repository.

You will get an answer within 7 days. Please include the affected version, steps to
reproduce and the impact.

## Scope and design notes

- The web UI and API listen on `127.0.0.1` only and require a random per-install token.
- The Free password is stored in the macOS Keychain (desktop), a `chmod 600` file (Termux)
  or encrypted with an Android Keystore AES-GCM key (app). It is never sent anywhere but
  `imap.free.fr` / `smtp.free.fr` over TLS (certificates verified).
- Mail HTML is rendered in a sandboxed iframe with a strict CSP; remote images are blocked
  until the user allows them.
- APK updates are only accepted by Android when signed with the release key.
