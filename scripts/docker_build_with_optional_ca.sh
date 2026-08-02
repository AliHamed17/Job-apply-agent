#!/bin/sh
# Git must keep this helper LF-only because it runs inside Linux build stages.
set -eu

secret_path=/run/secrets/enterprise_ca
bundle_path=

cleanup() {
    if [ -n "$bundle_path" ]; then
        rm -f "$bundle_path"
    fi
}
trap cleanup EXIT HUP INT TERM

if [ -f "$secret_path" ]; then
    if grep -q -- 'PRIVATE KEY' "$secret_path"; then
        echo 'Enterprise CA build secret must not contain a private key.' >&2
        exit 64
    fi
    if grep -q -- '-----BEGIN CERTIFICATE-----' "$secret_path"; then
        if [ ! -r /etc/ssl/certs/ca-certificates.crt ]; then
            echo 'System CA bundle is unavailable.' >&2
            exit 69
        fi
        bundle_path=$(mktemp /tmp/job-agent-ca-bundle.XXXXXX.pem)
        cat /etc/ssl/certs/ca-certificates.crt "$secret_path" > "$bundle_path"
        export PIP_CERT="$bundle_path"
        export SSL_CERT_FILE="$bundle_path"
        export REQUESTS_CA_BUNDLE="$bundle_path"
        export CURL_CA_BUNDLE="$bundle_path"
        export NODE_EXTRA_CA_CERTS="$secret_path"
    elif ! grep -q -- 'JOB_APPLY_AGENT_NO_ENTERPRISE_CA' "$secret_path"; then
        echo 'Enterprise CA build secret is not a PEM certificate bundle.' >&2
        exit 65
    fi
fi

"$@"
