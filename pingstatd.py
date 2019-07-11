#!/usr/bin/env python

# home: https://github.com/rndbit/pingstatd

# MIT License
#
# Copyright (c) 2018-2019 rndbit
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
# It:
#  * reads from stdin the data produced by ping on its stdout.
#  * listens on a specified TCP or UNIX or ABSTRACT socket to provide results.
#  * can act as a client when started with '-g' or '--get' arg to connect to
#    the daemon to collect the results.
#
# Examples of starting a daemon:
#  $ ping -f -i 5 example.com | pingstatd.py --tcp 127.0.1.1:2345
#  $ pingstatd.py --unix /var/run/pingstatd/example.com < <( ping -f -i 5 example.com )
#  $ pingstatd.py --abstract pingstatd/example.com < <( ping -f -i 5 example.com )
#
# Examples of collecting counts
#  $ pingstatd --get --tcp localhost:2345
#  $ pingstatd --unix /var/run/pingstatd/example.com --get
#  $ pingstatd --abstract /var/run/pingstatd/example.com -g
#  $ cat < /dev/tcp/127.0.1.1/2345
#  $ nc 127.0.1.1 2345
#  $ socat ABSTRACT-CONNECT:pingstatd/example.com -


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
import optparse


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


    def poll_register(self, poll):
        raise Exception("method not implemented: poll_register")


class PollHelper(object):
    def __init__(self):
        self.poll = select.poll()
        self.fd_handlers = {}


    def get_fd_count(self):
        return len(self.fd_handlers)


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

    _MATCH_HEADER_END = '\n'
    _MATCH_PING_END = '\n'
    _MATCH_REQUEST = '.'
    _MATCH_RESPONSE_OK = '\x08\x20\x08'
    _MATCH_RESPONSE_ERR = '\x08E'
    _MATCH_BELL = '\x07'

    _poll_flags = ( 0
            | select.POLLIN
            | select.POLLERR
    )


    def __init__(self, ping_output):

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

        self._PING_MATCH_ACTIONS = {
            self._MATCH_REQUEST:
                self.handle_ping_request,
            self._MATCH_RESPONSE_OK:
                self.handle_ping_response,
            self._MATCH_RESPONSE_ERR:
                self.handle_ping_response_error,
            self._MATCH_BELL:
                self.handle_ping_bell,
            self._MATCH_PING_END:
                self.handle_ping_end,
        }


    def poll_register(self, poll):
        poll.register(
                self.ping_output.fileno(),
                self._poll_flags,
                self,
                )


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
                message("read_ping loop pass, data=\"\\x:%s\"" % (binascii.hexlify(self.data.encode('utf-8'))))

            for match, action in self._PING_MATCH_ACTIONS.items():
                if self.data.startswith(match):
                    self.data = self.data[len(match):]
                    action()
                    break
            else:
                # executed if the inner for loop did not break, i.e. did not match anything
                message("handling garbage")
                self.handle_ping_garbage()


    def handle_ping_request(self):
                self.ping_count += 1
                if (VERB_LEVEL >= VERB_TRACE):
                    message("PING --> ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))


    def handle_ping_response(self):
                self.pong_count += 1
                if (VERB_LEVEL >= VERB_TRACE):
                    message("PONG <-- ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))


    def handle_ping_response_error(self):
                self.error_count += 1
                if (VERB_LEVEL >= VERB_TRACE):
                    message("ERROR    ping_count=%d, pong_count=%d, error_count=%d" % (self.ping_count, self.pong_count, self.error_count))


    def handle_ping_bell(self):
                if (VERB_LEVEL >= VERB_TRACE):
                    message("DROP bell")


    def handle_ping_end(self):
                if (VERB_LEVEL >= VERB_TRACE):
                    message("EOL, ending?!")
                self.state = 2
                self.read_footer()


    def handle_ping_garbage(self):
            # If reached here then the buffer contains unexpected content

            # Try to recover
            # Find the first sequence of interest
            restart_index = len(self.data)

            for match in self._PING_MATCH_ACTIONS:
                find_index = self.data.find(match, 1)
                if find_index > 0:
                    restart_index = min(restart_index, find_index)

            # Here restart_index is either the offset of the first byte sequence of interest
            # or it is equal to the length of the buffer (nothing of interest, discard all)

            if restart_index <= 0: # WTF
                if (VERB_LEVEL >= VERB_VERBOSE0):
                    message("Failed to handle garbage, restart index=%d, data=%s" % (
                            restart_index,
                            binascii.hexlify(self.data.encode('utf-8')),
                    ))
                sys.exit(1)

            if (VERB_LEVEL >= VERB_VERBOSE0):
                message("Discarding unexpected content len=%d of total %d bytes: \\x\"%s\"" % (
                        restart_index,
                        len(self.data),
                        binascii.hexlify(self.data[0:restart_index].encode('utf-8')),
                ))
            self.data = self.data[restart_index:]

            if (VERB_LEVEL >= VERB_VERBOSE0):
                if len(self.data) > 0:
                    message("Buffer after discarded content len=%d: \\x\"%s\"" % (
                            len(self.data),
                            binascii.hexlify(self.data.encode('utf-8')),
                    ))
                else:
                    message("Buffer is empty after unexpected content discarded")


    def read_footer(self):
        # TODO find LF first
        if (VERB_LEVEL >= VERB_TRACE):
            message("read footer: byte_count=%d data=\"%s\"" % (len(self.data), self.data))
        self.data = ''


class ServerSocketHandler(PollEventHandler):
    def __init__(self, socket, ping_proc):
        _MAX_CONNECTION_BACKLOG = 1

        self.socket = socket
        self.socket.listen(_MAX_CONNECTION_BACKLOG)
        self.socket.setblocking(0)

        self.ping_proc = ping_proc

        self.start_time = time.time()


    def poll_register(self, poll):
        poll.register(
                self.socket.fileno(),
                select.POLLIN | select.POLLERR,
                self,
                )


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

        cs_handler = ClientSocketHandler(
                client_socket,
                address,
                payload,
                )

        cs_handler.poll_register(poll)



class ClientSocketHandler(PollEventHandler):
    _poll_flags = ( 0
              | select.POLLOUT
              | select.POLLERR
    )

    def __init__(self, client_socket, address, payload):
        self.socket = client_socket
        self.address = address
        self.payload = payload

        self.socket.setblocking(0)


    def poll_register(self, poll):
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
                message("Closing fd=%d socket_peer=%s due to event type POLLHUP" % (
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


def parse_tcp_address(value):
    match = re.match(r'^(?P<host>.*):(?P<port>[0-9]+)$', value)
    if match is None:
        msg = 'Could not parse host:port from: "%s"' % (value)
        sys.stderr.write(msg + '\n')
        if (VERB_LEVEL >= VERB_WARN):
            message(msg)
        sys.exit(1)
    else:
        host = match.group('host')
        port = (int)(match.group('port'))

        if port <= 0 or port >= (2**16):
            msg = 'TCP port out of range: %d' % (port)
            sys.stderr.write(msg + '\n')
            if (VERB_LEVEL >= VERB_WARN):
                message(msg)
            sys.exit(1)

        return ( host, port )


op = optparse.OptionParser(
        usage='usage: %prog [options]',
        )
op.add_option('-t', '--tcp', dest='tcp_socket',
        help='use tcp socket; <host>:<port> e.g. localhost:2345',
        default=None,
        )
op.add_option('-u', '--unix', dest='unix_socket',
        help='use unix domain socket; <path>, e.g. /tmp/pintstatdr_example.com',
        default=None,
        )
op.add_option('-a', '--abstract', dest='abstract_socket',
        help='use abstract socket; <name>, e.g. pintstatdr_example.com',
        default=None,
        )
op.add_option('-g', '--get', dest='get', action="store_true",
        help='connect to deamon and output values, do not start a daemon',
        default=False,
        )
op.add_option('-v', dest='verb_level', action="count",
        help='increase verbosity',
        default=VERB_WARN,
        )

(options, args) = op.parse_args()

VERB_LEVEL = options.verb_level

# If working as a client
if options.get:
    dest = None
    if options.tcp_socket is not None:
        dest = parse_tcp_address(options.tcp_socket)
        c_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    elif options.unix_socket is not None:
        dest = options.unix_socket
        c_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    elif options.abstract_socket is not None:
        dest = "\0" + options.abstract_socket
        c_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        c_socket.connect(dest)
    except socket.error, ex:
        sys.stderr.write(str(ex) + "\n")
        sys.exit(1)

    data = c_socket.recv(4096)
    while data:
        sys.stdout.write(data)
        data = c_socket.recv(4096)

# If working as a daemon
else:
    bind = None
    if options.tcp_socket is not None:
        bind = parse_tcp_address(options.tcp_socket)
        s_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    elif options.unix_socket is not None:
        bind = options.unix_socket
        if os.path.exists(options.unix_socket):
            os.unlink(options.unix_socket)
        s_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    elif options.abstract_socket is not None:
        bind = "\0" + options.abstract_socket
        s_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        s_socket.bind(bind)
    except socket.error, ex:
        sys.stderr.write(str(ex) + "\n")
        sys.exit(1)

    poll = PollHelper()

    ping = PingOutputHandler(
        sys.stdin,
        )

    server_socket = ServerSocketHandler(
        s_socket,
        ping,
        )

    ping.poll_register(poll)
    server_socket.poll_register(poll)

    while poll.get_fd_count() > 0:
        poll.poll_dispatch()
