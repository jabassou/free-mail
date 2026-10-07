#!/usr/bin/env bash
# Throwaway IMAPS server for the test suite (Dovecot, self-signed certificate, any password = "secret").
# Needs root because Dovecot drops privileges:   sudo tests/dovecot.sh start | stop
set -euo pipefail
D="${FM_TEST_DIR:-/tmp/fm-dovecot}"
PORT="${FM_IMAP_PORT:-1993}"

if [ "${1:-start}" = "stop" ]; then
  [ -f "$D/run/master.pid" ] && kill "$(cat "$D/run/master.pid")" 2>/dev/null || true
  exit 0
fi
command -v dovecot >/dev/null || { echo "dovecot missing (apt-get install dovecot-imapd)" >&2; exit 1; }
id dovecot >/dev/null 2>&1 || { echo "system user 'dovecot' missing" >&2; exit 1; }
[ -f "$D/run/master.pid" ] && kill "$(cat "$D/run/master.pid")" 2>/dev/null && sleep 1 || true
rm -rf "$D"; mkdir -p "$D/run" "$D/state" "$D/mail"
openssl req -x509 -newkey rsa:2048 -nodes -days 3 -subj "/CN=127.0.0.1" \
  -keyout "$D/key.pem" -out "$D/cert.pem" >/dev/null 2>&1
chown -R dovecot:dovecot "$D/mail"
cat > "$D/dovecot.conf" <<EOF
protocols = imap
listen = 127.0.0.1
base_dir = $D/run
state_dir = $D/state
log_path = $D/dovecot.log
default_internal_user = dovecot
default_login_user = dovenull
default_internal_group = dovecot
mail_location = maildir:$D/mail/%u
mail_uid = dovecot
mail_gid = dovecot
first_valid_uid = 1
ssl = yes
ssl_cert = <$D/cert.pem
ssl_key = <$D/key.pem
auth_mechanisms = plain login
passdb {
  driver = static
  args = password=secret
}
userdb {
  driver = static
  args = uid=dovecot gid=dovecot home=$D/mail/%u
}
service imap-login {
  inet_listener imap {
    port = 0
  }
  inet_listener imaps {
    port = $PORT
    ssl = yes
  }
}
namespace inbox {
  inbox = yes
  separator = /
  mailbox Sent {
    special_use = \Sent
    auto = subscribe
  }
  mailbox Trash {
    special_use = \Trash
    auto = subscribe
  }
  mailbox Junk {
    special_use = \Junk
    auto = subscribe
  }
  mailbox Drafts {
    special_use = \Drafts
    auto = subscribe
  }
}
EOF
dovecot -c "$D/dovecot.conf"
for _ in $(seq 50); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then echo "dovecot listening on 127.0.0.1:$PORT"; exit 0; fi
  sleep 0.2
done
tail -20 "$D/dovecot.log" >&2; exit 1
