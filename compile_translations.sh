#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DOMAIN='me.vorotnikov.Obozrenie'
LOCALEDIR="$PROJECT_ROOT/obozrenie/data/locale"

cd "$PROJECT_ROOT"
uv run pybabel compile -D "$DOMAIN" -d "$LOCALEDIR"
