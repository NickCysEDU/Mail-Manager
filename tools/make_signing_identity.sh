#!/usr/bin/env bash
# Create a local code-signing identity, so the app keeps the same identity
# across rebuilds.
#
# Why this exists: an ad-hoc signature (codesign -s -) has no identity of its
# own; macOS identifies it by the hash of its contents, which changes every
# time anything is rebuilt. The Keychain remembers permission per identity, so
# with ad-hoc signing "Always Allow" is forgotten on the next build and the
# password prompt comes back. A certificate, even a self-signed one, gives a
# fixed identity and the decision sticks.
#
# What this does NOT do: make the app trusted by Gatekeeper. That needs a paid
# Apple Developer ID and notarisation. A first launch still needs right-click
# then Open, exactly as before. This changes nothing about how much the app is
# trusted - only whether macOS can tell one build from the next.
#
#   ./tools/make_signing_identity.sh          # create it
#   ./tools/make_signing_identity.sh --remove # delete it again
#
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${MAILMANAGER_SIGN_NAME:-Mail Manager Local Signing}"
KEYCHAIN="${HOME}/Library/Keychains/login.keychain-db"

G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$G" "$N" "$*" >&2; }
warn() { printf '%swarning:%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

if [[ "${1:-}" == "--remove" ]]; then
  say "Removing “$NAME”"
  security delete-identity -c "$NAME" "$KEYCHAIN" 2>/dev/null || true
  security delete-certificate -c "$NAME" "$KEYCHAIN" 2>/dev/null || true
  echo "Removed. Builds will go back to ad-hoc signing."
  exit 0
fi

if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$NAME"; then
  say "Already present: $NAME"
  security find-identity -v -p codesigning | grep -F "$NAME"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say "Creating a self-signed code-signing certificate"
cat > "$WORK/openssl.cnf" <<'CONF'
[ req ]
distinguished_name = dn
x509_extensions    = codesign
prompt             = no

[ dn ]
CN = PLACEHOLDER

[ codesign ]
basicConstraints       = critical,CA:false
keyUsage               = critical,digitalSignature
extendedKeyUsage       = critical,codeSigning
subjectKeyIdentifier   = hash
CONF
sed -i '' "s/CN = PLACEHOLDER/CN = $NAME/" "$WORK/openssl.cnf"

openssl req -newkey rsa:2048 -nodes -x509 -days 3650 \
  -config "$WORK/openssl.cnf" \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null \
  || die "openssl could not create the certificate"

# macOS's security tool reads the older PKCS#12 encryption; OpenSSL 3 writes a
# newer one by default that it rejects with a confusing "wrong password".
PASS="$(openssl rand -hex 16)"
openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
  -macalg sha1 -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES \
  -passout "pass:$PASS" -out "$WORK/identity.p12" 2>/dev/null \
  || openssl pkcs12 -export -legacy -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
       -passout "pass:$PASS" -out "$WORK/identity.p12" 2>/dev/null \
  || die "openssl could not package the identity"

say "Adding it to your login keychain"
# -T codesign lets codesign use the key without asking every single time.
security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$PASS" \
  -T /usr/bin/codesign -T /usr/bin/security >/dev/null \
  || die "could not import the identity into the login keychain"

# Trust it for code signing. This is a user-level trust setting: it says "I
# made this certificate and I am willing to run things I signed with it". It
# does not affect any other certificate, or anything else on the Mac.
say "Marking it as trusted for signing code (your keychain only)"
if ! security add-trusted-cert -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem" 2>/dev/null; then
  warn "Could not set the trust flag automatically."
  warn "Open Keychain Access, find “$NAME”, Get Info, and set"
  warn "Code Signing to Always Trust. Then run this again to check."
fi

if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$NAME"; then
  say "Ready."
  security find-identity -v -p codesigning | grep -F "$NAME"
  echo
  echo "  ./build_app.sh will use it automatically from now on."
  echo "  Remove it any time with: ./tools/make_signing_identity.sh --remove"
else
  die "the identity was created but codesign cannot see it yet"
fi
