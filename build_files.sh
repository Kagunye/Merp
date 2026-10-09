#!/usr/bin/env bash
# Vercel build step: install deps, then collect static files into
# staticfiles_build/static so the @vercel/static builder can serve them.
set -euo pipefail

echo "BUILD: installing python dependencies"
python3.12 -m pip install --upgrade pip
python3.12 -m pip install -r requirements.txt

echo "BUILD: running collectstatic"
export DJANGO_SETTINGS_MODULE="config.settings.vercel"
# A placeholder key is enough for collectstatic — it never serves traffic.
export SECRET_KEY="${SECRET_KEY:-build-time-placeholder}"
export ALLOWED_HOSTS="${ALLOWED_HOSTS:-*}"
python3.12 manage.py collectstatic --noinput --clear
