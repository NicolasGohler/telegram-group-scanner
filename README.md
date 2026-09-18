# Telegram Group Scanner

Signal intelligence for Web3 Telegram communities.

Monitors configured Telegram groups for buying intent, founder activity, and fundraising signals. Scores messages with GPT-4o-mini, cross-references Twitter/X engagement metrics, and delivers a curated weekly digest to Slack.

Built at [MPM Labs](https://mpmlabs.xyz) to surface warm outbound targets from communities where ICP founders and investors are active.

## How it works

1. Connects to configured Telegram groups via Telethon
2. Scores and categorizes messages for lead intent using GPT-4o-mini
3. Enriches identified profiles with Twitter/X engagement data
4. Delivers a weekly signal digest to Slack

## Setup

Add these to your GitHub repository secrets:

```
OPENAI_API_KEY      GPT-4o-mini for message scoring
SLACK_BOT_TOKEN     Slack bot token
TWITTER_API_KEY     TwitterAPI.io for profile enrichment
```

Telegram session credentials are generated once via `generate_session.py` and stored as a secret.

## Stack

Python · Telethon · OpenAI · Slack SDK · TwitterAPI.io · GitHub Actions
