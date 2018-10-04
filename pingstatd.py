#!/usr/bin/env python

import subprocess
import binascii
import os
import select
import fcntl
import sys
import socket
import traceback
import errno


_EVENT_LOOKUP = {
    select.EPOLLIN: 'POLLIN',
    select.EPOLLOUT: 'POLLOUT',
    select.EPOLLPRI: 'POLLPRI',
    select.EPOLLERR: 'POLLERR',
    select.EPOLLHUP: 'POLLHUP',
}


def debug(msg):
    sys.stderr.write(msg)
    sys.stderr.write('\n')
    pass


class PollEventHandler(object):
    def handle_poll_event(self, epoll, fd, events):
        raise Exception("method not implemented: handle_poll_event")


class Epoll(object):
    def __init__(self):
        self.epoll = select.epoll()
        self.fd_handlers = {}


    def register(self, fd, flags, handler = None):
        self.epoll.register(fd, flags)
        self.fd_handlers[fd] = handler


    def unregister(self, fd):
        self.epoll.unregister(fd)
        del self.fd_handlers[fd]


    def poll(self):
        poll_results = self.epoll.poll()

        if len(poll_results) == 0:
            debug("Empty poll result")
            return

        for fdwork in poll_results:
            fd = fdwork[0]
            events = fdwork[1]
            handler = self.fd_handlers[fd]
            debug("Unblocked fd={0}, events={1}=[{2}]".format(fd, events, _get_flag_names(events)))
            try:
                handler.handle_poll_event(self, fd, events)
            except SystemExit as ex:
                raise ex
            except BaseException as ex:
                debug("Exception invoking handler in Epoll.poll: {0}".format(traceback.format_exc()))

    def modify(self, fd, flags):
        self.epoll.modify(fd, flags)


class PingProc(subprocess.Popen, PollEventHandler):
    epoll_flags = ( 0
            | select.EPOLLIN
            | select.EPOLLERR
            | select.EPOLLONESHOT
    )

    def __init__(self, host, interval, epoll):

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

        ping_args = [
            '/bin/ping',
            '-f',
          # Debug, remove later
#          '-c', '3', # Count of packets, exit after those
#          '-a',
            '-i', str(interval),
            '-n', host,
        ]
        super(PingProc, self).__init__(
            args = ping_args,
            bufsize = 0,
            stdout = subprocess.PIPE)

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

        epoll.register(
                self.stdout.fileno(),
                self.epoll_flags,
                self)


    def handle_poll_event(self, epoll, fd, events):

        if events & select.EPOLLIN == select.EPOLLIN:
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
                raise Exception('Unpexted state: {0}'.format(self.state))

        if events & select.EPOLLHUP == select.EPOLLHUP:
            debug("err event")
            # Flush and destroy
            self.communicate()
            sys.exit(0)

        epoll.modify(
                self.stdout.fileno(),
                self.epoll_flags)


    def read_header(self):
        lf_index = self.data.find('\n')
        if (lf_index > -1):
            header = self.data[0:lf_index]
            debug("Read header: \"{0}\"".format(header))

            self.data = self.data[lf_index+1:]
            self.state = 1

            if len(self.data) > 0:
                data_hex = binascii.hexlify(self.data)
                debug("Data remains after reading header, pass to read_header(): \"{0}\"".format(data_hex))
                self.read_ping()


    def read_ping(self):
        debug("read_ping")

        while len(self.data) > 0:
            debug("read_ping loop pass")

            if self.data.startswith(b'.'):
                self.ping_count += 1
                self.data = self.data[1:]
                debug("PING --> ping_count={0}, pong_count={1}, error_count={2}".format(self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith(b'\x08\x20\x08'):
                self.pong_count += 1
                self.data = self.data[3:]
                debug("PONG <-- ping_count={0}, pong_count={1}, error_count={2}".format(self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith(b'\x08E'):
                self.error_count += 1
                self.data = self.data[2:]
                debug("ERROR    ping_count={0}, pong_count={1}, error_count={2}".format(self.ping_count, self.pong_count, self.error_count))
                continue
            if self.data.startswith(b'\x07'):
                self.data = self.data[1:]
                debug("DROP bell")
                continue
            if self.data.startswith(b'\n'):
                self.data = self.data[1:]
                debug("EOL, ending?!")
                self.state = 2
                self.read_footer()
                return

            debug("Unexpected content, need to kill?: \"\\x:{0}\"".format(self.data))
            # Try to recover
            restart_index = len(self.data)

            find_index = self.data.find(b'.', 1)
            if find_index > 0:
                restart_index = min(restart_index, find_index)

            find_index = self.data.find(b'\x08', 1)
            if find_index > 0:
                restart_index = min(restart_index, find_index)

            find_index = self.data.find(b'\x07', 1)
            if find_index > 0:
                restart_index = min(restart_index, find_index)

            debug("Discarding unexpected content: len={0} \\x\"{1}\", \"{2}\"".format(
                    restart_index,
                    self.data[0:restart_index],
                    self.data[0:restart_index])
            )
            self.data = self.data[restart_index:]
            debug("Keeping after unexpected content: \\x\"{0}\", \"{1}\"".format(self.data, self.data))


    def read_footer(self):
        # TODO find LF first
        debug("read footer: byte_count={0} data=\"{1}\"".format(len(self.data), self.data))


class ServerSocket(PollEventHandler):
    def __init__(self, ip, port, ping_proc, epoll):
        _MAX_CONNECTION_BACKLOG = 1

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((ip, port))
        self.socket.listen(_MAX_CONNECTION_BACKLOG)
        self.socket.setblocking(0)

        epoll.register(
                self.socket.fileno(),
                select.EPOLLIN | select.EPOLLERR,
                self)

        self.ping_proc = ping_proc


    def handle_poll_event(self, epoll, fd, events):
        client_socket, address = self.socket.accept()
        payload = ( ""
                + "request_count={0}\n"
                + "response_count={1}\n"
                + "error_count={2}\n"
                + "host={3}\n"
                + "address={4}\n"
            ).format(
                    self.ping_proc.ping_count,
                    self.ping_proc.pong_count,
                    self.ping_proc.error_count,
                    "TODO",
                    "TODO")

        ClientSocketHandler(
                client_socket,
                address,
                payload,
                epoll)



class ClientSocketHandler(PollEventHandler):
    _epoll_flags = ( 0
              | select.EPOLLOUT
              | select.EPOLLERR
              | select.EPOLLONESHOT
    )

    def __init__(self, client_socket, address, payload, epoll):
        self.socket = client_socket
        self.address = address
        self.payload = payload

        self.socket.setblocking(0)

        self.send()
        if self.payload is not None:
            epoll.register(
                    self.socket.fileno(),
                    self.epoll_flags,
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
            debug("Exception={1} send data: {0}".format(traceback.format_exc(), type(ex).__name__))
            # trigger shutdown
            self.payload = None

        if sent_count < len(self.payload):
            debug("Sent bytes={0} of {1}".format(sent_count, len(self.payload)))
            self.payload = self.payload[sent_count:]
        else:
            self.payload = None
            debug("Sent ALL byte_count={0}, closed".format(sent_count))


    def handle_poll_event(self, epoll, fd, events):

        if events & select.EPOLLHUP == select.EPOLLHUP:
            debug("event type EPOLLHUP, closing")
            epoll.unregister(self.socket.fileno())
            self.socket.close()
            return

        if events & select.EPOLLOUT == select.EPOLLOUT:
            self.send()
            if self.payload is not None:
                debug("More to send, re-arming epoll")
                epoll.modify(
                        self.socket.fileno(),
                        self.epoll_flags)
            else:
                debug("Nothing to send, closing")
                epoll.unregister(self.socket.fileno())
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


epoll = Epoll()

ping = PingProc(
        host = sys.argv[1],
        interval = 0.5,
        epoll = epoll)

server_socket = ServerSocket(
        'google.ping.mrtg',
        3000,
        ping,
        epoll)

while True:
    poll_results = epoll.poll()
