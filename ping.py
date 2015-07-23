#!/usr/bin/env python

import subprocess
import sys
import threading


class Pinger(object):
    status = []  # Populated while we are running
    hosts = []  # List of all hosts/ips in our input queue

    # How many ping process at the time.
    thread_count = 100

    # Lock object to keep track the threads in loops, where it can potentially be race conditions.
    lock = threading.Lock()

    def ping(self, ip):
        """Pings the requested server. Note: this function is not threaded yet, therefore pinging may take up to a second."""
        if sys.platform == 'win32':
            ping_cmd = ["ping", '-n', '1', ip]
        else:
            ping_cmd = ["ping", '-c', '1', '-n', '-W', '1', ip]

        ping_output, _ = subprocess.Popen(ping_cmd, universal_newlines=True, stdout=subprocess.PIPE).communicate()

        try:
            if sys.platform == 'win32':
                rtt_info = round(float(ping_output.rstrip('\n').split('\n')[-1].split(',')[0].split('=')[-1].strip('ms')))
            else:
                rtt_info = round(float(ping_output.split('\n')[1].split('=')[-1].split(' ')[0]))
        except:
            rtt_info = 9999

        return rtt_info

    def pop_queue(self):
        ip = None

        self.lock.acquire()  # Grab or wait+grab the lock.

        if self.hosts:
            ip = self.hosts.pop()

        self.lock.release()  # Release the lock, so another thread could grab it.

        return ip

    def dequeue(self):
        while True:
            ip = self.pop_queue()

            if not ip:
                return None

            result = self.ping(ip)
            self.status[0].append(ip)
            self.status[0].append(result)

    def start(self):
        threads = []
        self.status.append([])

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
