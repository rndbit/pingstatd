#!/usr/bin/env python

# home: https://github.com/rndbit/pingstatd

# MIT License
#
# Copyright (c) 2018 rndbit
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import subprocess
import binascii
import os
import select
import fcntl
import sys
import socket
import traceback
import errno
import time


_EVENT_LOOKUP = {
    select.POLLIN: 'POLLIN',
    select.POLLOUT: 'POLLOUT',
    select.POLLPRI: 'POLLPRI',
    select.POLLERR: 'POLLERR',
    select.POLLHUP: 'POLLHUP',
}


def debug(msg):
#    sys.stderr.write(msg)
#    sys.stderr.write('\n')
    pass


class PollEventHandler(object):
    def handle_poll_event(self, poll, fd, events):
        raise Exception("method not implemented: handle_poll_event")


class Poll(object):
    def __init__(self):
        self.poll = select.poll()
        self.fd_handlers = {}


    def register(self, fd, flags, handler = None):
        self.poll.register(fd, flags)
        self.fd_handlers[fd] = handler


    def unregister(self, fd):
        self.poll.unregister(fd)
        del self.fd_handlers[fd]


    def poll_dispatch(self):
        poll_results = self.poll.poll()

        if len(poll_results) == 0:
            debug("Empty poll result")
            return

        for fdwork in poll_results:
            fd = fdwork[0]
            events = fdwork[1]
            handler = self.fd_handlers[fd]
            debug("Unblocked fd=%s, events=%s=[%r]" % (fd, events, _get_flag_names(events)))
            try:
                handler.handle_poll_event(self, fd, events)
            except SystemExit:
                raise
            except:
                debug("Exception invoking handler in Poll.poll: %s" % (traceback.format_exc()))


#class PingOutputHandler(subprocess.Popen, PollEventHandler):
class PingOutputHandler(PollEventHandler):
    _poll_flags = ( 0
            | select.POLLIN
            | select.POLLERR
    )

    #def __init__(self, host, interval, poll):
    def __init__(self, ping_output, poll):

        '''
        0 - created, reading header line to get started
        1 - expecting markers for ICMP packets or linefeed for bailing out
        2 - flushing tail stats and exiting
'''
        self.state = 0;

        '''
        The data left over from previous read,
        but not attributed to any message
        should get handled when more data arrives
        '''
        self.data = ''

        self.ping_count = 0
        self.pong_count = 0
        self.error_count = 0

        """
        ping_args = [
            '/bin/ping',
            '-f',
          # Debug, remove later
#          '-c', '3', # Count of packets, exit after those
#          '-a',
            '-i', str(interval),
            '-n', host,
        ]
        super(PingOutputHandler, self).__init__(
            args = ping_args,
            bufsize = 0,
            stdout = subprocess.PIPE)
"""

        self.stdout = ping_output

        ### Set non-blocking
        # Get the already set flags
        flags = fcntl.fcntl(
                self.stdout.fileno(),
                fcntl.F_GETFL)
        # Change flags by adding NONBLOCK
        fcntl.fcntl(
                self.stdout.fileno(),
                fcntl.F_SETFL,
                flags | os.O_NONBLOCK)

        poll.register(
                self.stdout.fileno(),
                self._poll_flags,
                self)


    def handle_poll_event(self, poll, fd, events):

        if events & select.POLLIN == select.POLLIN:
            debug("read event")

            data = None
            try:
                data = self.stdout.read()
            except IOError:
                debug("exception reading pings markers")
                return

#            data_old_hex = binascii.hexlify(self.data)
#            data_new_hex = binascii.hexlify(data)
#            debug("data old+new: {0}+{1}".format(data_old_hex, data_new_hex))

            self.data += data

            if self.state == 0:
                self.read_header()
            elif self.state == 1:
                # do read flood bytes or tail
                self.read_ping()
            elif self.state == 2:
                # do finish up
                self.read_footer()
                pass
            else:
                raise Exception('Unpexted state: %d' % (self.state))

        if events & select.POLLHUP == select.POLLHUP:
            debug("err event")
            # Flush and destroy
#            self.communicate()
            sys.exit(0)


    def read_header(self):
        lf_index = self.data.find('\n')
        if (lf_index > -1):
            header = self.data[0:lf_index]
            debug("Read header: \"%s\"" % (header))

            self.data = self.data[lf_index+1:]
            self.state = 1

            if len(self.data) > 0:
                data_hex = binascii.hexlify(self.data)
                debug("Data remains after reading header, pass to read_header(): \"%s\"" % (data_hex))
                self.read_ping()


    def read_ping(self):
        debug("read_ping")

        while len(self.data) > 0:
            debug("read_ping loop pass")

            if self.data.startswith('.'):
                self.ping_count += 1
                self.data = self.data[1:]
                debug("PING --> ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith('\x08\x20\x08'):
                self.pong_count += 1
                self.data = self.data[3:]
                debug("PONG <-- ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith('\x08E'):
                self.error_count += 1
                self.data = self.data[2:]
                debug("ERROR    ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith('\x07'):
                self.data = self.data[1:]
                debug("DROP bell")
                continue
            if self.data.startswith('\n'):
                self.data = self.data[1:]
                debug("EOL, ending?!")
                self.state = 2
                self.read_footer()
                return

            debug("Unexpected content, need to kill?: \"\\x:%s\"" % (self.data))
            # Try to recover
            restart_index = len(self.data)

            find_index = self.data.find('.', 1)
            if find_index > 0:
                restart_index = min(restart_index, find_index)

            find_index = self.data.find('\x08', 1)
            if find_index > 0:
                restart_index = min(restart_index, find_index)

            find_index = self.data.find('\x07', 1)
            if find_index > 0:
                restart_index = min(restart_index, find_index)

            debug("Discarding unexpected content: len=%d \\x\"%s\", \"%s\"" % (
                    restart_index,
                    self.data[0:restart_index],
                    self.data[0:restart_index])
            )
            self.data = self.data[restart_index:]
            debug("Keeping after unexpected content: \\x\"%s\", \"%s\"" % (self.data, self.data))


    def read_footer(self):
        # TODO find LF first
        debug("read footer: byte_count=%d data=\"%s\"" % (len(self.data), self.data))


class ServerSocketHandler(PollEventHandler):
    def __init__(self, ip, port, ping_proc, poll):
        _MAX_CONNECTION_BACKLOG = 1

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((ip, port))
        self.socket.listen(_MAX_CONNECTION_BACKLOG)
        self.socket.setblocking(0)

        poll.register(
                self.socket.fileno(),
                select.POLLIN | select.POLLERR,
                self)

        self.ping_proc = ping_proc

        self.start_time = time.time()


    def handle_poll_event(self, poll, fd, events):
        now_time = time.time()
        uptime = int(now_time - self.start_time)

        client_socket, address = self.socket.accept()
        payload = ( ""
                + "request_count=%d\n"
                + "response_count=%d\n"
                + "error_count=%d\n"
                + "host=%s\n"
                + "address=%s\n"
                + "uptime=%d\n"
            ) % (
                    self.ping_proc.ping_count,
                    self.ping_proc.pong_count,
                    self.ping_proc.error_count,
                    "TODO",
                    "TODO",
                    uptime)

        ClientSocketHandler(
                client_socket,
                address,
                payload,
                poll)



class ClientSocketHandler(PollEventHandler):
    _poll_flags = ( 0
              | select.POLLOUT
              | select.POLLERR
    )

    def __init__(self, client_socket, address, payload, poll):
        self.socket = client_socket
        self.address = address
        self.payload = payload

        self.socket.setblocking(0)

        self.send()
        if self.payload is not None:
            poll.register(
                    self.socket.fileno(),
                    self._poll_flags,
                    self)
        else:
#            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()


    def send(self):
        sent_count = None
        try:
            sent_count = self.socket.send(self.payload)
        except socket.error, ex:
            if ex.args[0] == errno.EWOULDBLOCK:
                debug("Hit would-block, ignoring")
                return
            debug("Exception=%s send data: %s" % (
                    type(ex).__name__,
                    traceback.format_exc(),
            ))
            # trigger shutdown
            self.payload = None

        if sent_count < len(self.payload):
            debug("Sent bytes=%d of %d" % (sent_count, len(self.payload)))
            self.payload = self.payload[sent_count:]
        else:
            self.payload = None
            debug("Sent ALL byte_count=%d, closed" % (sent_count))


    def handle_poll_event(self, poll, fd, events):

        if events & select.POLLHUP == select.POLLHUP:
            debug("event type POLLHUP, closing")
            poll.unregister(self.socket.fileno())
            self.socket.close()
            return

        if events & select.POLLOUT == select.POLLOUT:
            self.send()
            if self.payload is None:
                debug("Nothing to send, closing")
                poll.unregister(self.socket.fileno())
#                self.socket.shutdown(socket.SHUT_RDWR)
                self.socket.close()


def _get_flag_names(flags):
    names = []
    for bit, name in _EVENT_LOOKUP.items():
        if flags & bit:
            names.append(name)
            flags -= bit
 
            if flags == 0:
                break

    return names


if len(sys.argv) != 3:
    print("invalid args, need: prog bind_host bind_to_port")
    sys.exit(1)

bind_host = sys.argv[1]
bind_port = sys.argv[2]
try:
    bind_port = (int)(bind_port)
except:
    print("invalid bind_port: %d" % (bind_port))
    sys.exit(1)

if bind_port <= 0 or bind_port >= (2**16):
    print("bind_port out of range: %d" % (bind_port))
    sys.exit(1)

poll = Poll()

ping = PingOutputHandler(
        sys.stdin,
#        host = sys.argv[1],
#        interval = 0.5,
        poll = poll)

server_socket = ServerSocketHandler(
        bind_host,
        bind_port,
        ping,
        poll)

while True:
    poll_results = poll.poll_dispatch()
