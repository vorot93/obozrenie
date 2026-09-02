#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DOMAIN='me.vorotnikov.Obozrenie'
LOCALEDIR="$PROJECT_ROOT/obozrenie/data/locale"

cd "$PROJECT_ROOT"
uv run pybabel extract -k N_ -o "$LOCALEDIR/$DOMAIN.pot" obozrenie
uv run python "$PROJECT_ROOT/tools/add_ui_messages.py"
uv run pybabel update \
    --ignore-obsolete \
    --no-fuzzy-matching \
    -i "$LOCALEDIR/$DOMAIN.pot" \
    -D "$DOMAIN" \
    -d "$LOCALEDIR"
uv run python "$PROJECT_ROOT/tools/normalize_catalogs.py"
