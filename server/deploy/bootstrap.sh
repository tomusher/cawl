#!/bin/sh
# Prepare the database and Traefik's shared dynamic configuration, then start.
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# This also creates the Compose network and establishes the project name.
docker compose up -d postgres
postgres=$(docker compose ps -q postgres)
network=$(docker inspect -f '{{range $name := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$postgres" | head -n 1)
project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$postgres")
password=$(docker compose exec -T postgres printenv POSTGRES_PASSWORD)

# Run migrations
docker run --rm --network "$network" --env-file .env \
  -e "CAWL_DATABASE_URL=postgresql://cawl:${password}@postgres:5432/cawl" \
  ghcr.io/tomusher/cawl-server:main python manage.py migrate --noinput

# Seed the volume Traefik watches. The image runs as the cawl user, so routes
# generated later by the daemon retain the required write access.
volume="${project}_traefik-dynamic"
docker volume create "$volume" >/dev/null
docker run --rm \
  -v "$volume:/var/lib/cawl/dynamic" \
  -v "$PWD/traefik/control-plane.yml:/tmp/control-plane.yml:ro" \
  ghcr.io/tomusher/cawl-server:main \
  /bin/sh -c 'cp /tmp/control-plane.yml /var/lib/cawl/dynamic/control-plane.yml'

docker compose up -d
