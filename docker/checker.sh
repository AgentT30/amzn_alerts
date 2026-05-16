#!/bin/sh
set -eu

interval="${CHECK_INTERVAL_SECONDS:-3600}"

while true; do
  python amazon_price_check.py check
  sleep "$interval"
done
