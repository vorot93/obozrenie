#!/usr/bin/env python

import subprocess
import sys
import threading

import obozrenie.helpers as helpers


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

        ping_output, _ = subprocess.Popen(ping_cmd, stdout=subprocess.PIPE).communicate()

        try:
            if sys.platform == 'win32':
                rtt_info = round(float(ping_output.rstrip('\n').split('\n')[-1].split(',')[0].split('=')[-1].strip('ms')))
            else:
                rtt_info = round(float(ping_output.split('\n')[1].split('=')[-1].split(' ')[0]))
        except:
            rtt_info = 9999

        return rtt_info

    def stat_qstat(self, entry):
        qstat_cmd = ["qstat", "-xml", "-utf8", ' '.join(self.options), entry.strip()]

        qstat_output, _ = subprocess.Popen(qstat_cmd, stdout=subprocess.PIPE).communicate()

        return qstat_output

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
            elif self.action == "qstat":
                result = self.stat_qstat(entry)

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
