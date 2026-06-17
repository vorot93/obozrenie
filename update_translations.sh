#!/bin/sh
DOMAIN='me.vorotnikov.Obozrenie'
LOCALEDIR='locale'
pybabel extract -o $LOCALEDIR/$DOMAIN.pot obozrenie
pybabel update -i $LOCALEDIR/$DOMAIN.pot -D $DOMAIN -d $LOCALEDIR
