# Polly Telegram Integration

## Overview

Polly connects to Telegram through the Bot API using webhook callbacks.

## Webhook setup

Register the webhook with `POLLY_WEBHOOK_URL` pointing at `/telegram/webhook`.
Polly validates updates with the bot token and routes commands to the poll engine.
