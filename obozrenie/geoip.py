#!/usr/bin/env python3
# This source file is part of Obozrenie
# Copyright 2015 Artem Vorotnikov

# For more information, see https://github.com/obozrenie/obozrenie

# Obozrenie is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.

# Obozrenie is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with Obozrenie.  If not, see <http://www.gnu.org/licenses/>.

"""GeoIP database discovery and download for Obozrenie."""

import os

import requests
from xdg import BaseDirectory

DOWNLOAD_URL = "https://cdn.jsdelivr.net/npm/@ip-location-db/dbip-geo-whois-asn-country-mmdb/dbip-geo-whois-asn-country.mmdb"

KNOWN_SYSTEM_PATHS = [
    "/usr/share/GeoIP/dbip-country.mmdb",
    "/usr/share/GeoIP/dbip-country-lite.mmdb",
    "/usr/share/GeoIP/GeoLite2-Country.mmdb",
    "/var/lib/GeoIP/GeoLite2-Country.mmdb",
]

CACHE_DB_PATH = os.path.join(
    BaseDirectory.xdg_cache_home, "obozrenie", "dbip-country.mmdb")

_CHUNK_SIZE = 65536


def find_database():
    """Return the first readable database path, or None.

    Probes the known system locations in order, then the downloaded
    cache copy. First match wins.
    """
    for path in KNOWN_SYSTEM_PATHS + [CACHE_DB_PATH]:
        if os.path.isfile(path) and os.access(path, os.R_OK):
            return path
    return None


def _remove_quiet(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download_database(cancel_event, progress_cb=None):
    """Download the country database to the cache, atomically.

    Streams to a temporary file. Checks ``cancel_event`` on each chunk;
    if set, deletes the partial file and returns None. On success, the
    file is validated as a geoip2 database, moved into place, and its
    path returned. Returns None on any network/IO/validation error.
    """
    import geoip2.database
    import maxminddb

    os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)
    tmp_path = CACHE_DB_PATH + ".part"
    try:
        with requests.get(DOWNLOAD_URL, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            with open(tmp_path, "wb") as handle:
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    if cancel_event.is_set():
                        _remove_quiet(tmp_path)
                        return None
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        progress_cb(downloaded, total)
        with geoip2.database.Reader(tmp_path):
            pass
        os.replace(tmp_path, CACHE_DB_PATH)
        return CACHE_DB_PATH
    except (requests.RequestException, OSError,
            maxminddb.InvalidDatabaseError):
        _remove_quiet(tmp_path)
        return None
