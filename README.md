# pingstatd
read piped ping output and keep sums, e.g. for mrtg

This program is for keeping counts of sent and received ping packets.

It is designed to work together with `ping` executable, started with -f arg.

It reads from stdin the output that `ping` produces to stdout.

e.g. start with

    ping -f -i 5 example.com | pingstatd.py 127.0.1.1 PORT

The counts can be collected by connecting to it via TCP.

e.g. collect counts

    nc 127.0.1.1 PORT
