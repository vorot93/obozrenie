#!/bin/sh
DOMAIN='io.obozrenie'
LOCALEDIR='locale'
python3 setup.py extract_messages -o $LOCALEDIR/$DOMAIN.pot
python3 setup.py update_catalog -i $LOCALEDIR/$DOMAIN.pot -D $DOMAIN -d $LOCALEDIR
