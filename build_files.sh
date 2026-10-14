#!/usr/bin/env bash
# Vercel build step: install deps, collect static files into
# staticfiles_build/static (served by the @vercel/static builder), and run
# migrations if DATABASE_URL points at a reachable DB.
set -euo pipefail

echo "BUILD: installing python dependencies"
python3.12 -m pip install --upgrade pip
python3.12 -m pip install -r requirements.txt

export DJANGO_SETTINGS_MODULE="config.settings.vercel"
# A placeholder key is enough for build-time commands — they never serve traffic.
export SECRET_KEY="${SECRET_KEY:-build-time-placeholder}"
export ALLOWED_HOSTS="${ALLOWED_HOSTS:-*}"

echo "BUILD: running collectstatic"
python3.12 manage.py collectstatic --noinput --clear

echo "BUILD: attempting migrations (non-fatal)"
python3.12 manage.py migrate --noinput || echo "BUILD: migrations skipped or failed; app will boot without them"

echo "BUILD: ensuring default admin user exists (non-fatal)"
python3.12 manage.py create_default_admin || echo "BUILD: admin seed skipped (no DB attached yet)"

if [ "${SEED_DEMO:-0}" = "1" ]; then
  echo "BUILD: seeding demo dataset (SEED_DEMO=1)"
  python3.12 manage.py seed_demo || echo "BUILD: demo seed skipped (DB unreachable)"
fi
