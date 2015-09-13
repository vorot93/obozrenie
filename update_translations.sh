#!/bin/sh
DOMAIN='io.obozrenie'
LOCALEDIR='locale'
python setup.py extract_messages -o $LOCALEDIR/$DOMAIN.pot
python setup.py update_catalog -i $LOCALEDIR/$DOMAIN.pot -D $DOMAIN -d $LOCALEDIR
