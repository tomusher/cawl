#!/bin/sh
# Configure a first deployment, then prepare its database and Traefik config.
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

new_env=0
if [ ! -f .env ]; then
  cp .env.example .env
  new_env=1
fi

value_of() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' .env
}

set_value() {
  key=$1 value=$2 tmp=$(mktemp)
  awk -v key="$key" -v value="$value" '
    $0 ~ "^" key "=" { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' .env >"$tmp"
  mv "$tmp" .env
}

random_secret() {
  openssl rand -hex 32 2>/dev/null || od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

prompt_value() {
  key=$1 label=$2 default=$3
  if [ ! -t 0 ]; then
    echo "$key must be configured in .env before running bootstrap.sh" >&2
    exit 1
  fi
  printf '%s [%s]: ' "$label" "$default" >&2
  IFS= read -r value || true
  value=${value:-$default}
  [ -n "$value" ] || { echo "$key cannot be empty" >&2; exit 1; }
  set_value "$key" "$value"
}

# Prompt once for the deployment settings. The marker also makes an existing
# hand-written .env go through this confirmation on its first bootstrap run.
configured=$(value_of CAWL_BOOTSTRAPPED)
for spec in \
  'CAWL_API_DOMAIN|Control-plane API domain|cawl.example.com' \
  'CAWL_PUBLIC_DOMAIN|Public sandbox domain|public.example.com' \
  'CAWL_INCUS_URL|Incus API URL|https://host.docker.internal:8443' \
  'TRAEFIK_ACME_EMAIL|ACME email address|ops@example.com' \
  'TRAEFIK_DNS_PROVIDER|ACME DNS provider|cloudflare'; do
  key=${spec%%|*}; rest=${spec#*|}; label=${rest%%|*}; default=${rest#*|}
  current=$(value_of "$key")
  if [ "$new_env" -eq 1 ] || [ "$configured" != 1 ]; then
    prompt_value "$key" "$label" "${current:-$default}"
  fi
done

for key in POSTGRES_PASSWORD CAWL_SECRET_KEY; do
  current=$(value_of "$key")
  case $current in
    ''|replace-with-*)
      if [ -t 0 ]; then
        printf 'Generate %s? [Y/n]: ' "$key" >&2
        IFS= read -r answer || true
        case $answer in n|N|no|NO) prompt_value "$key" "$key" "" ;; *) set_value "$key" "$(random_secret)" ;; esac
      else
        echo "$key must be set in .env before running bootstrap.sh" >&2
        exit 1
      fi
      ;;
  esac
done

# Provider credentials vary by DNS provider. Prompt for every credential that
# remains an example placeholder (for example CF_DNS_API_TOKEN).
for key in $(awk -F= '/^[A-Za-z_][A-Za-z0-9_]*=replace-with-/{print $1}' .env); do
  prompt_value "$key" "$key" ""
done
set_value CAWL_BOOTSTRAPPED 1

# Compose does not interpolate bind-mounted configuration files. Render the
# static Traefik configuration from the configured domains and ACME settings.
sed \
  -e "s|\${CAWL_API_DOMAIN}|$(value_of CAWL_API_DOMAIN)|g" \
  -e "s|\${CAWL_PUBLIC_DOMAIN}|$(value_of CAWL_PUBLIC_DOMAIN)|g" \
  -e "s|\${TRAEFIK_ACME_EMAIL}|$(value_of TRAEFIK_ACME_EMAIL)|g" \
  -e "s|\${TRAEFIK_DNS_PROVIDER}|$(value_of TRAEFIK_DNS_PROVIDER)|g" \
  traefik/traefik.yml.template >traefik/traefik.yml

# This also creates the Compose network and establishes the project name.
docker compose up -d postgres
postgres=$(docker compose ps -q postgres)
network=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$postgres" | head -n 1)
password=$(docker compose exec -T postgres printenv POSTGRES_PASSWORD)

# Keep migrations out of the long-running Compose services.
docker run --rm --network "$network" --env-file .env \
  -e "CAWL_DATABASE_URL=postgresql://cawl:${password}@postgres:5432/cawl" \
  ghcr.io/tomusher/cawl-server:main python manage.py migrate --noinput

docker compose up -d
docker compose exec -T cawl python manage.py sync_ingress
