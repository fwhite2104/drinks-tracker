#!/bin/sh
# Container entrypoint.
#
# supercronic crashes with "Failed to fork exec" when it runs as the
# container's PID 1, so scheduled services keep a shell in front of it
# (sh stays PID 1, supercronic is its child; docker stop falls back to
# SIGKILL after the grace period, which is fine for scheduled collection).
#
# Any other command (docker compose run / one-off admin commands) is exec'd
# directly so it runs as intended, streams output, and returns its real exit
# code. The previous `ENTRYPOINT ["/bin/sh", "-c"]` split multi-arg command
# overrides into sh arguments, so `compose run collector python -m ...`
# silently ran a bare `python` REPL and exited 0 without doing anything.
#
# docker compose passes a `command:` string as ONE argument while exec-form
# CMD arrives as separate arguments; match on the joined command so both
# shapes reach the scheduler branch.
set -eu
command_line="$*"
case "$command_line" in
    supercronic\ *|supercronic)
        sh -c "$command_line"
        ;;
    *)
        exec "$@"
        ;;
esac
