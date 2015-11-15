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

import subprocess
import sys
import threading

import obozrenie.helpers as helpers


def add_rtt_info(array):
    """Appends server response time to the table."""
    hosts_array = []
    rtt_array = []
    rtt_array.append([])

    for entry in array:
        host = entry['host'].split(":")
        if len(host) > 1:
            host = ":".join(host[0:-1])
        else:
            host = ":".join(host)
        hosts_array.append(host)

    pinger = Pinger()
    pinger.hosts = list(set(hosts_array))
    pinger.action = "ping"

    pinger.status.clear()
    rtt_array = pinger.start()

    # Match ping in host list.
    for entry in array:
        host = entry['host'].split(":")
        if len(host) > 1:
            host = ":".join(host[0:-1])
        else:
            host = ":".join(host)
        entry["ping"] = rtt_array[host]


class Pinger():
    status = {}  # Populated while we are running
    hosts = []  # List of all hosts/ips in our input queue

    action = "ping"
    options = []

    # How many ping process at the time.
    thread_count = 100

    # Lock object to keep track the threads in loops, where it can potentially be race conditions.
    lock = threading.Lock()

    def ping(self, entry):
        """Pings the requested server. Note: this function is not threaded yet, therefore pinging may take up to a second."""
        if sys.platform == 'win32':
            ping_cmd = ["ping", '-n', '1', entry]
        else:
            ping_cmd = ["ping", '-c', '1', '-n', '-W', '1', entry]

        ping_output_byte, _ = subprocess.Popen(ping_cmd, stdout=subprocess.PIPE).communicate()
        ping_output = ping_output_byte.decode()

        try:
            if sys.platform == 'win32':
                rtt_info = ping_output.rstrip('\n').split('\n')[-1].split(',')[0].split('=')[-1].strip('ms')
            else:
                rtt_info = ping_output.split('\n')[1].split('=')[-1].split(' ')[0]

            rtt_num = round(float(rtt_info))
        except:
            rtt_num = 9999

        return rtt_num

    def pop_queue(self):
        entry = None

        self.lock.acquire()  # Grab or wait+grab the lock.

        if self.hosts:
            entry = self.hosts.pop()

        self.lock.release()  # Release the lock, so another thread could grab it.

        return entry

    def dequeue(self):
        while True:
            entry = self.pop_queue()

            if not entry:
                return None

            if self.action == "ping":
                result = self.ping(entry)
            else:
                result = None

            self.status[entry] = result

    def start(self):
        threads = []

        for i in range(self.thread_count):
            # Create self.thread_count number of threads that together will
            # cooperate removing every ip in the list. Each thread will do the
            # job as fast as it can.
            t = threading.Thread(target=self.dequeue)
            t.start()
            threads.append(t)

        # Wait until all the threads are done. .join() is blocking.
        [t.join() for t in threads]

        return self.status
