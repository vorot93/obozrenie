#!/bin/sh
DOMAIN='io.obozrenie'
LOCALEDIR='locale'
python3 setup.py compile_catalog -D $DOMAIN -d $LOCALEDIR
