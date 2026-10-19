#!/usr/bin/env bash
# Install only immutable tools before a backup job receives its database secret.
set -euo pipefail
: "${RUNNER_TEMP:?A disposable CI workspace is required}"
: "${GITHUB_PATH:?GitHub Actions PATH file is required}"
POSTGRES_IMAGE='postgres:18.6-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c'
AGE_VERSION='v1.3.2'
AGE_SHA256='cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10'
TOOLS_DIR=$(mktemp -d "$RUNNER_TEMP/proam-backup-tools-XXXXXX")
chmod 700 "$TOOLS_DIR"
docker pull "$POSTGRES_IMAGE"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "https://github.com/FiloSottile/age/releases/download/$AGE_VERSION/age-$AGE_VERSION-linux-amd64.tar.gz" \
    --output "$TOOLS_DIR/age.tar.gz"
printf '%s  %s\n' "$AGE_SHA256" "$TOOLS_DIR/age.tar.gz" | sha256sum --check --strict
tar -xzf "$TOOLS_DIR/age.tar.gz" -C "$TOOLS_DIR" age/age age/age-keygen
mv "$TOOLS_DIR/age/age" "$TOOLS_DIR/age-bin"
mv "$TOOLS_DIR/age/age-keygen" "$TOOLS_DIR/age-keygen"
rmdir "$TOOLS_DIR/age"
mv "$TOOLS_DIR/age-bin" "$TOOLS_DIR/age"
rm "$TOOLS_DIR/age.tar.gz"
cat > "$TOOLS_DIR/pg-client" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
: "${RUNNER_TEMP:?A disposable CI workspace is required}"
case "${0##*/}" in
    psql|pg_dump|pg_restore|createdb|dropdb) command_name=${0##*/} ;;
    *) echo 'Unsupported PostgreSQL client command' >&2; exit 1 ;;
esac
exec docker run --rm --network host --interactive --user "$(id -u):$(id -g)" \
    --volume "$RUNNER_TEMP:$RUNNER_TEMP" --volume "$PWD:$PWD:ro" --workdir "$PWD" \
    --env PGPASSWORD --env PGSSLMODE \
    'postgres:18.6-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c' \
    "$command_name" "$@"
WRAPPER
chmod 700 "$TOOLS_DIR/pg-client"
for command_name in psql pg_dump pg_restore createdb dropdb; do
    ln -s pg-client "$TOOLS_DIR/$command_name"
done
printf '%s\n' "$TOOLS_DIR" >> "$GITHUB_PATH"
"$TOOLS_DIR/pg_dump" --version
"$TOOLS_DIR/age" --version
