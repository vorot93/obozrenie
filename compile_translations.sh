#!/bin/sh
DOMAIN='io.obozrenie'
LOCALEDIR='locale'
python setup.py compile_catalog -D $DOMAIN -d $LOCALEDIR
