#!/bin/sh
DOMAIN='me.vorotnikov.Obozrenie'
LOCALEDIR='locale'
python3 setup.py compile_catalog -D $DOMAIN -d $LOCALEDIR
