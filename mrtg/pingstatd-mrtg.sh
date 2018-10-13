#!/bin/bash

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

# This script gets values from pingstatd.py daemon and makes them available
# in format expected by MRTG

##############################################################################

ARGS=( "$@" )
ARGS=( "${ARGS[@]:1}" )

declare -r DAEMON_HOST=${ARGS[0]}
declare -r DAEMON_PORT=${ARGS[1]}

declare -r PING_HOST=${ARGS[2]}
declare -r PING_INTERVAL=${ARGS[3]}

##############################################################################

# This is the target host's IP address as reported by `ping` output.
# Initialise the value to something clearly obvious when it failed to obtain
# the real value from the pingstatd.py daemon, e.g. on first run.
address='unknown'

INDATA=UNKNOWN
OUTDATA=UNKNOWN
UPTIME_SECONDS=0

if vars=$( nc ${DAEMON_HOST} ${DAEMON_PORT} ) ;
then
  <<-EXAMPLE
	request_count=12
	response_count=12
	error_count=0
	host=example.com
	address=1.2.3.4
	uptime=11
	EXAMPLE

  # validate $vars content for safety to be `eval`ed
  if grep -vqE "^ *[_[:alnum:]]*=(([\"'])?)[-_\.[:alnum:]]*\1$" <<<"${vars}";
  then # Not Safe
    (
      IFS=$'\n'
      echo "Refusing to eval result:"
      printf "\t%s\n" ${vars}
    ) 1>&2
  else # Safe to eval
    eval "${vars}"
    INDATA=$(( request_count - response_count ))
    OUTDATA=${request_count}
    UPTIME_SECONDS=${uptime}
  fi
else # failed to obtain values from the pingstatd.py daemon
  # need to start
  echo starting pingstat_${PING_HOST} 1>&2

  # connecting stdin with this construct '< <()' has a minute advantage that
  # `pingstatd.py` ends up being a child process of `logger`
  # and `ping` the child process of `pingstatd.py`
  # which makes easier to work out which process belongs to which group
  # as opposed to when using a simple pipeline `ping | pingstatd.py | logger`
  # where all 3 process become orphaned by the shell and adopted by `init`
  logger -t "pingstatd_${PING_HOST}" < <(
    SCRIPT_DIR=$( d=$( readlink -f "${BASH_SOURCE[0]}" ); echo "${d%/*}" )
    PATH="${PATH}:${SCRIPT_DIR}:."

    exec pingstatd.py ${DAEMON_HOST} ${DAEMON_PORT} 2>&1 < <(
      exec ping -f -i ${PING_INTERVAL} ${PING_HOST}
    )
  ) &

fi

printf -v TARGET_NAME \
    "ping %s (%s) every %d seconds" \
    "${PING_HOST}" "${address}" "${PING_INTERVAL}"

printf "%s\n%s\n%s\n%s\n" \
    "${INDATA}" \
    "${OUTDATA}" \
    "${UPTIME_SECONDS}" \
    "${TARGET_NAME}"
