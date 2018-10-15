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

##############################################################################

# This program is for keeping counts of sent and received ping packets.
#
# It is designed to work together with `ping` executable, started with -f arg.
# It reads ping's stdout via pipe from its stdin.
# The counts can be collected by connecting to it via TCP.
#   e.g. start with
# ping -f -i 5 example.com | pingstatd.py 127.0.1.1 PORT
#   collect counts
# nc 127.0.1.1 PORT


import binascii
import os
import select
import fcntl
import sys
import socket
import traceback
import errno
import time
import re


_EVENT_LOOKUP = {
    select.POLLIN: 'POLLIN',
    select.POLLOUT: 'POLLOUT',
    select.POLLPRI: 'POLLPRI',
    select.POLLERR: 'POLLERR',
    select.POLLHUP: 'POLLHUP',
}


# available levels
VERB_NONE = 0
VERB_ERR = 1
VERB_WARN = 2
VERB_INFO = 3
VERB_VERBOSE0 = 5
VERB_VERBOSE1 = 6
VERB_VERBOSE2 = 7
VERB_TRACE = 9

# The current level
# TODO set from args e.g. -v
VERB_LEVEL = VERB_WARN

def message(msg):
    sys.stdout.write(msg)
    sys.stdout.write('\n')


class PollEventHandler(object):
    def handle_poll_event(self, poll, fd, events):
        raise Exception("method not implemented: handle_poll_event")


class PollHelper(object):
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
            if (VERB_LEVEL >= VERB_TRACE):
                message("Empty poll result")
            return

        for fdwork in poll_results:
            fd = fdwork[0]
            events = fdwork[1]
            handler = self.fd_handlers[fd]
            if (VERB_LEVEL >= VERB_TRACE):
                message("Unblocked fd=%s, events=%s=[%r]" % (fd, events, _get_flag_names(events)))
            try:
                handler.handle_poll_event(self, fd, events)
            except SystemExit:
                raise
            except:
                if (VERB_LEVEL >= VERB_ERR):
                    message("Exception invoking handler in PollHelper.poll_dispatch: %s" % (traceback.format_exc()))


class PingOutputHandler(PollEventHandler):
    _poll_flags = ( 0
            | select.POLLIN
            | select.POLLERR
    )

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
        self.host = 'unknown'
        self.address = 'unknown'

        self.ping_output = ping_output

        ### Set non-blocking
        # Get the already set flags
        flags = fcntl.fcntl(
                self.ping_output.fileno(),
                fcntl.F_GETFL)
        # Change flags by adding NONBLOCK
        fcntl.fcntl(
                self.ping_output.fileno(),
                fcntl.F_SETFL,
                flags | os.O_NONBLOCK)

        poll.register(
                self.ping_output.fileno(),
                self._poll_flags,
                self)


    def handle_poll_event(self, poll, fd, events):

        if events & select.POLLIN == select.POLLIN:
            if (VERB_LEVEL >= VERB_TRACE):
                message("read event")

            data = None
            try:
                data = self.ping_output.read()
            except IOError:
                if (VERB_LEVEL >= VERB_WARN):
                    message("exception reading ping markers")
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
            if (VERB_LEVEL >= VERB_TRACE):
                message("POLLHUP event")
            sys.exit(0)


    def read_header(self):
        lf_index = self.data.find('\n')
        if (lf_index > -1):
            header = self.data[0:lf_index]
            if (VERB_LEVEL >= VERB_TRACE):
                message("Read header: \"%s\"" % (header))

            match = re.search(r'^PING ([^ ]*) *\(([^\)]*)\)', header)
            if match is None:
                msg = 'Could not match target IP address from ping output'
                sys.stderr.write(msg + '\n')
                if (VERB_LEVEL >= VERB_WARN):
                    message(msg)
            else:
                self.host = match.group(1)
                self.address = match.group(2)
                if (VERB_LEVEL >= VERB_INFO):
                    message('From header matched: hostname=%s address=%s' % (
                            self.host,
                            self.address,
                    ))

            self.data = self.data[lf_index+1:]
            self.state = 1

            if len(self.data) > 0:
                data_hex = binascii.hexlify(
                        self.data.encode('utf-8'))
                if (VERB_LEVEL >= VERB_TRACE):
                    message("Data remains after reading header, pass to read_header(): \"%s\"" % (data_hex))
                self.read_ping()


    def read_ping(self):
        if (VERB_LEVEL >= VERB_TRACE):
            message("read_ping")

        while len(self.data) > 0:
            if (VERB_LEVEL >= VERB_TRACE):
                message("read_ping loop pass")

            if self.data.startswith('.'):
                self.ping_count += 1
                self.data = self.data[1:]
                if (VERB_LEVEL >= VERB_TRACE):
                    message("PING --> ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith('\x08\x20\x08'):
                self.pong_count += 1
                self.data = self.data[3:]
                if (VERB_LEVEL >= VERB_TRACE):
                    message("PONG <-- ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith('\x08E'):
                self.error_count += 1
                self.data = self.data[2:]
                if (VERB_LEVEL >= VERB_TRACE):
                    message("ERROR    ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith('\x07'):
                self.data = self.data[1:]
                if (VERB_LEVEL >= VERB_TRACE):
                    message("DROP bell")
                continue
            if self.data.startswith('\n'):
                self.data = self.data[1:]
                if (VERB_LEVEL >= VERB_TRACE):
                    message("EOL, ending?!")
                self.state = 2
                self.read_footer()
                return

            if (VERB_LEVEL >= VERB_WARN):
                message("Unexpected content, need to kill?: \"\\x:%s\"" % (self.data))
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

            if (VERB_LEVEL >= VERB_WARN):
                message("Discarding unexpected content: len=%d \\x\"%s\", \"%s\"" % (
                        restart_index,
                        self.data[0:restart_index],
                        self.data[0:restart_index])
                )
            self.data = self.data[restart_index:]

            if (VERB_LEVEL >= VERB_TRACE):
                message("Keeping after unexpected content: \\x\"%s\", \"%s\"" % (self.data, self.data))


    def read_footer(self):
        # TODO find LF first
        if (VERB_LEVEL >= VERB_TRACE):
            message("read footer: byte_count=%d data=\"%s\"" % (len(self.data), self.data))


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

        if (VERB_LEVEL >= VERB_VERBOSE0):
            message("Accepted fd=%d socket_peer=%s" % (
                    client_socket.fileno(),
                    str(client_socket.getpeername()),
            ))

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
                    self.ping_proc.host,
                    self.ping_proc.address,
                    uptime)
        payload = payload.encode('utf-8')

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
            ex_errno = ex.args[0]
            ex_message = ex.args[1]
            # It should be impossible for errno to be errno.EAGAIN (or errno.EWOULDBLOCK)
            # because this gets called when poll() notifies of event select.POLLOUT
            if ex_errno == errno.EAGAIN or ex_errno == errno.EWOULDBLOCK:
                if (VERB_LEVEL >= VERB_TRACE):
                    message("Hit would-block, ignoring")
                return
            if (VERB_LEVEL >= VERB_TRACE):
                message("Exception=%s send data: %s" % (
                        type(ex).__name__,
                        traceback.format_exc(),
                ))
            # trigger this socket close and remove
            self.payload = None
            # TODO log ex_message
            return

        if sent_count < len(self.payload):
            if (VERB_LEVEL >= VERB_TRACE):
                message("Sent bytes=%d of %d" % (sent_count, len(self.payload)))
            self.payload = self.payload[sent_count:]
        else:
            self.payload = None
            if (VERB_LEVEL >= VERB_TRACE):
                message("Sent ALL byte_count=%d, closed" % (sent_count))


    def handle_poll_event(self, poll, fd, events):

        if events & select.POLLHUP == select.POLLHUP:
            if (VERB_LEVEL >= VERB_VERBOSE0):
                message("Closing fd=%d socket_peer=%s due to event type POLLHUP: %s" % (
                        self.socket.fileno(),
                        str(self.socket.getpeername()),
                ))
            poll.unregister(self.socket.fileno())
            self.socket.close()
            return

        if events & select.POLLOUT == select.POLLOUT:
            self.send()
            if self.payload is None:
                if (VERB_LEVEL >= VERB_VERBOSE0):
                    message("Closing fd=%d socket_peer=%s" % (
                            self.socket.fileno(),
                            str(self.socket.getpeername()),
                    ))
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

poll = PollHelper()

ping = PingOutputHandler(
        sys.stdin,
        poll = poll)

server_socket = ServerSocketHandler(
        bind_host,
        bind_port,
        ping,
        poll)

while True:
    poll.poll_dispatch()
