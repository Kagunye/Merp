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
