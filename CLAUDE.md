# TG Lead Generation System — Context for Claude Code

## What This Project Does

This is an automated lead generation pipeline for **MPM Labs**, a Web3 marketing consultancy. It scans Telegram group chats daily for project announcements, enriches those leads with Twitter/website/Discord data, and delivers a scored digest to Slack. Nicolas then manually crafts personalized outreach based on the enriched data.

**Outreach is always manual and never automated.** This system only automates the research and qualification step.

## Business Context

MPM Labs has two verticals:
1. **Web3-native retainers** — crypto projects needing marketing/growth services. Leads come from Telegram groups and Twitter. Most are early-stage and too small for Apollo.
2. **"2Web3" program** — traditional companies entering Web3. Leads come from Apollo searches (handled by the separate Fundraising Agent).

This project serves vertical #1.

## Pipeline Architecture

```
scanner.py → GPT-4o-mini analysis → Slack digest (plain text)
           → leads.json (structured data)
                ↓
           enricher.py → Twitter metrics (TwitterAPI.io)
                       → Website scrape (requests + BS4)
                       → Discord member count
                       → GPT synthesis (fit score 1-5)
                       → Slack enriched digest (Block Kit)
                       → enriched_leads.json
```

Runs daily at **9:00 CET (8:00 UTC)** on GitHub Actions, plus manual `workflow_dispatch`.

## What Makes a Good Lead

**Include:**
- Web3 projects with a sustainable underlying business model
- B2B startups with a scalable product
- Founder introductions with a real product
- Fundraise announcements, product launches, partnerships, milestones

**Exclude:**
- Memecoins and speculative tokens without real business models
- Freelancers/consultants offering their services
- Agencies selling custom/bespoke services
- Generic service pitches without a distinct product

Key distinction: a startup has a product that clients use, while an agency/freelancer sells custom labor.

## Nicolas's Manual Workflow (After Receiving Digest)

1. Reviews the enriched Slack digest
2. For interesting leads, clicks the TG link to see the original message
3. The TG message often links to a tweet (fundraise, launch, partnership)
4. Checks Twitter vanity metrics: follower count, engagement, activity
5. Follows the link in their Twitter bio to the project website
6. Checks Discord/TG community size
7. Decides whether to reach out
8. Crafts hyper-customized outreach DMs

## Outreach Style

Nicolas uses super casual DMs, typically 3-4 messages in a row:
- "Hey gm gm"
- "Saw your announcement in [group chat name]" (shortened naturally, e.g. "suihub")
- Something specific and insightful about the project — honest feedback, a question about their GTM, or a relevant connection ("I just spoke with another team building in prediction markets")

The CTA is always conversational ("can you tell me more about your GTM") rather than a hard pitch.

## File Structure

| File | Purpose |
|------|---------|
| `scanner.py` | Scans 13 TG groups, GPT analysis, Slack digest, writes leads.json |
| `enricher.py` | Reads leads.json, enriches with Twitter/website/Discord/GPT, sends enriched digest |
| `config.json` | TG group IDs and names, scan window, model selection |
| `requirements.txt` | Python dependencies |
| `generate_session.py` | One-time utility to generate Telegram session strings |
| `.github/workflows/scan.yml` | GitHub Actions workflow (daily + manual trigger) |

## TG Groups Scanned (13)

Abstract Builders, Crypto Horizon, Genesis Frens, GetFunded.Network, OffChain Bali, OffChain Global, Proof of Contribution, S21 Community, Solana Canada, Solana Montreal, SuiHub Europe, Swiss NFT Association, Web3 Montreal

These are configured in `config.json` with their numeric IDs.

## APIs & Services

| Service | Purpose | Key Location |
|---------|---------|-------------|
| Telegram (Telethon) | Read group messages | Hardcoded in scanner.py |
| OpenAI (GPT-4o-mini) | Message analysis + lead synthesis | Hardcoded in scanner.py/enricher.py |
| Slack | Digest delivery | Hardcoded, channel `C09LV45H77D` |
| TwitterAPI.io | Profile lookup + engagement metrics | Hardcoded in enricher.py |
| Discord public API | Server member counts from invite links | No key needed |

All keys are hardcoded (same pattern throughout the project — not using env vars or secrets manager).

## Related Projects

| Directory | What It Does |
|-----------|-------------|
| `A&GH - Fundraising Agent` | Scrapes CryptoRank/RootData fundraising data, enriches with Apollo, weekly Slack digest |
| `A&GH - Telegram Members List` | Fetches TG group member lists with bios, exports to CSV |
| `2W3 - Landing Page 12:05` | React landing page for the "Digital Asset Opportunity Mapper" tool |

## Key Preferences

- **Never automate outreach or message sending** — only automate research
- **Cost efficiency matters** — chose TwitterAPI.io ($0.15/1k) over official X API ($200/mo)
- **Skip CRM integration** (Monday.com) for now
- **Keep it lightweight** — no Playwright/headless browsers, requests + BS4 is sufficient
- **GitHub Actions free tier** — total pipeline must finish in < 10 minutes
- TG messages are non-standard (AMAs, X spaces, Discord invites, blog posts, websites) — the system needs to be as flexible as possible with URL extraction and classification
