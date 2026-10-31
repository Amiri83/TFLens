#!/bin/bash
# Cron entry for the TFLens watchdog (hermes cron --no-agent --script tflens_watch.sh).
exec bash "$(dirname "$(readlink -f "$0")")/watch.sh" TFLens
