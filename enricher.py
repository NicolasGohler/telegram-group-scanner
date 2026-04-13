"""
TG Lead Enrichment Pipeline

Reads leads.json produced by scanner.py, enriches each lead with Twitter metrics,
website scraping, Discord member counts, and GPT synthesis, then sends an enriched
digest to Slack using Block Kit formatting.
"""

import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# --- Config (from GitHub Actions secrets / environment) ---
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C09LV45H77D")
TWITTER_API_KEY = os.environ["TWITTER_API_KEY"]
MODEL = "gpt-4o-mini"

TIME_CEILING_SECONDS = 8 * 60  # 8 minutes hard ceiling
REQUEST_TIMEOUT = 10  # seconds per HTTP request

_start_time = None


def time_remaining():
    """Check if we're within the time ceiling."""
    if _start_time is None:
        return True
    return (time.time() - _start_time) < TIME_CEILING_SECONDS


# ---------------------------------------------------------------------------
# URL Classification
# ---------------------------------------------------------------------------

def classify_url(url):
    """Classify a URL into a category for routing enrichment."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")

    if domain in ("twitter.com", "x.com"):
        path = parsed.path.strip("/")
        parts = path.split("/")
        if len(parts) == 1 and parts[0]:
            return "twitter_profile", parts[0]
        if len(parts) >= 3 and parts[1] == "status":
            return "tweet", parts[0]
        return "twitter_other", path

    if domain == "discord.gg" or (domain == "discord.com" and "/invite/" in parsed.path):
        invite_code = parsed.path.strip("/").split("/")[-1]
        return "discord_invite", invite_code

    if domain == "t.me":
        return "telegram_link", parsed.path.strip("/")

    if domain == "linktr.ee":
        return "linktree", parsed.path.strip("/")

    if domain in ("github.com",):
        return "github", parsed.path.strip("/")

    return "website", url


# ---------------------------------------------------------------------------
# Twitter (TwitterAPI.io)
# ---------------------------------------------------------------------------

def fetch_twitter_profile(username):
    """Fetch Twitter profile info via TwitterAPI.io."""
    try:
        resp = requests.get(
            "https://api.twitterapi.io/twitter/user/info",
            params={"userName": username},
            headers={"X-API-Key": TWITTER_API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return None
        user = data.get("user", {})
        return {
            "username": user.get("userName", username),
            "name": user.get("name", ""),
            "followers": user.get("followersCount", 0),
            "following": user.get("followingCount", 0),
            "bio": user.get("description", ""),
            "website": user.get("website", ""),
            "verified": user.get("isBlueVerified", False),
            "created_at": user.get("createdAt", ""),
        }
    except Exception as e:
        print(f"  ⚠️  Twitter profile error for @{username}: {e}")
        return None


def fetch_twitter_engagement(username):
    """Fetch recent tweet engagement stats via TwitterAPI.io."""
    try:
        resp = requests.get(
            "https://api.twitterapi.io/twitter/user/last_tweets",
            params={"userName": username, "count": 10},
            headers={"X-API-Key": TWITTER_API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        tweets = data.get("tweets", [])
        if not tweets:
            return None

        total_likes = 0
        total_rts = 0
        total_replies = 0
        count = 0
        for tweet in tweets:
            total_likes += tweet.get("likeCount", 0)
            total_rts += tweet.get("retweetCount", 0)
            total_replies += tweet.get("replyCount", 0)
            count += 1

        if count == 0:
            return None

        return {
            "avg_likes": round(total_likes / count),
            "avg_retweets": round(total_rts / count),
            "avg_replies": round(total_replies / count),
            "tweets_analyzed": count,
        }
    except Exception as e:
        print(f"  ⚠️  Twitter engagement error for @{username}: {e}")
        return None


# ---------------------------------------------------------------------------
# Website Scraping
# ---------------------------------------------------------------------------

def scrape_website(url):
    """Scrape a website for title, description, and social links."""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadEnricher/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        description = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            description = meta_desc["content"].strip()
        if not description:
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            if og_desc and og_desc.get("content"):
                description = og_desc["content"].strip()

        # Find social links in page
        social_links = {"twitter": [], "discord": [], "telegram": [], "github": []}
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "twitter.com/" in href or "x.com/" in href:
                social_links["twitter"].append(href)
            elif "discord.gg/" in href or "discord.com/invite/" in href:
                social_links["discord"].append(href)
            elif "t.me/" in href:
                social_links["telegram"].append(href)
            elif "github.com/" in href:
                social_links["github"].append(href)

        # Deduplicate
        for key in social_links:
            social_links[key] = list(dict.fromkeys(social_links[key]))

        return {
            "title": title[:200],
            "description": description[:500],
            "social_links": social_links,
            "url": url,
        }
    except Exception as e:
        print(f"  ⚠️  Website scrape error for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def fetch_discord_info(invite_code):
    """Fetch Discord server info from a public invite."""
    try:
        resp = requests.get(
            f"https://discord.com/api/v10/invites/{invite_code}?with_counts=true",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        guild = data.get("guild", {})
        return {
            "server_name": guild.get("name", ""),
            "member_count": data.get("approximate_member_count", 0),
            "online_count": data.get("approximate_presence_count", 0),
        }
    except Exception as e:
        print(f"  ⚠️  Discord info error for invite {invite_code}: {e}")
        return None


# ---------------------------------------------------------------------------
# Lead Enrichment Orchestrator
# ---------------------------------------------------------------------------

def enrich_lead(lead):
    """Orchestrate enrichment for a single lead with multi-hop discovery."""
    enrichment = {
        "twitter": None,
        "twitter_engagement": None,
        "website": None,
        "discord": None,
        "discovered_urls": [],
    }

    urls = lead.get("urls_in_message", [])
    twitter_usernames = set()
    website_urls = []
    discord_codes = []

    # Step 1: Classify all URLs from the message
    for url in urls:
        url_type, value = classify_url(url)
        if url_type == "twitter_profile":
            twitter_usernames.add(value)
        elif url_type == "tweet":
            twitter_usernames.add(value)
        elif url_type == "discord_invite":
            discord_codes.append(value)
        elif url_type == "website":
            website_urls.append(value)

    # Step 2: Fetch Twitter profile + engagement for the first handle found
    if twitter_usernames and time_remaining():
        username = list(twitter_usernames)[0]
        profile = fetch_twitter_profile(username)
        if profile:
            enrichment["twitter"] = profile
            # Multi-hop: if Twitter bio has a website, add it for scraping
            bio_website = profile.get("website", "")
            if bio_website and bio_website not in website_urls:
                website_urls.append(bio_website)
                enrichment["discovered_urls"].append(("twitter_bio", bio_website))

        if time_remaining():
            engagement = fetch_twitter_engagement(username)
            if engagement:
                enrichment["twitter_engagement"] = engagement

    # Step 3: Scrape the first website found
    if website_urls and time_remaining():
        site_data = scrape_website(website_urls[0])
        if site_data:
            enrichment["website"] = site_data

            # Multi-hop: discover social links from website
            social = site_data.get("social_links", {})

            # Discover Twitter from website if we don't have one yet
            if not enrichment["twitter"] and social.get("twitter"):
                tw_url = social["twitter"][0]
                tw_type, tw_user = classify_url(tw_url)
                if tw_type == "twitter_profile" and time_remaining():
                    profile = fetch_twitter_profile(tw_user)
                    if profile:
                        enrichment["twitter"] = profile
                        enrichment["discovered_urls"].append(("website", tw_url))
                    if time_remaining():
                        engagement = fetch_twitter_engagement(tw_user)
                        if engagement:
                            enrichment["twitter_engagement"] = engagement

            # Discover Discord from website if we don't have one yet
            if not discord_codes and social.get("discord"):
                disc_url = social["discord"][0]
                disc_type, disc_code = classify_url(disc_url)
                if disc_type == "discord_invite":
                    discord_codes.append(disc_code)
                    enrichment["discovered_urls"].append(("website", disc_url))

    # Step 4: Fetch Discord info
    if discord_codes and time_remaining():
        discord_data = fetch_discord_info(discord_codes[0])
        if discord_data:
            enrichment["discord"] = discord_data

    return enrichment


# ---------------------------------------------------------------------------
# GPT Synthesis
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are a venture analyst evaluating a Telegram lead for an investor relations / growth agency.

Given the lead summary and enrichment data, provide:
1. A fit score from 1-5 (5 = excellent fit, active project with real community)
2. A 1-sentence assessment (max 20 words) covering the most important signal: legitimacy, community health, or growth potential

Key signals to look for:
- Follower count vs engagement ratio (high followers with no engagement = red flag)
- Active Discord/TG community
- Working website with clear product
- YC, major VC backing, or notable partnerships

Return ONLY valid JSON:
{"fit_score": 4, "assessment": "Short one-line assessment here.", "key_signals": ["signal1", "signal2"]}"""


def synthesize_lead(lead, enrichment):
    """Use GPT to synthesize enrichment data into a fit score and assessment."""
    context_parts = [f"Lead summary: {lead.get('summary', '')}"]
    context_parts.append(f"Group: {lead.get('group_name', '')}")
    context_parts.append(f"Message: {lead.get('message_text', '')[:300]}")

    tw = enrichment.get("twitter")
    if tw:
        context_parts.append(
            f"Twitter @{tw['username']}: {tw['followers']} followers, "
            f"bio: {tw.get('bio', '')[:200]}, verified: {tw.get('verified', False)}"
        )
    eng = enrichment.get("twitter_engagement")
    if eng:
        context_parts.append(
            f"Engagement (last {eng['tweets_analyzed']} tweets): "
            f"avg {eng['avg_likes']} likes, {eng['avg_retweets']} RTs"
        )
    web = enrichment.get("website")
    if web:
        context_parts.append(f"Website: {web.get('title', '')} — {web.get('description', '')[:200]}")
    disc = enrichment.get("discord")
    if disc:
        context_parts.append(
            f"Discord: {disc['server_name']} — {disc['member_count']} members, "
            f"{disc['online_count']} online"
        )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYNTHESIS_PROMPT},
                {"role": "user", "content": "\n".join(context_parts)},
            ],
            temperature=0.3,
            max_tokens=300,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        return json.loads(raw)
    except Exception as e:
        print(f"  ⚠️  Synthesis error: {e}")
        return {"fit_score": 0, "assessment": "Synthesis failed — manual review needed.", "key_signals": []}


# ---------------------------------------------------------------------------
# Slack Formatting
# ---------------------------------------------------------------------------

def format_slack_blocks(enriched_leads):
    """Format enriched leads into Slack Block Kit blocks."""
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Enriched Lead Digest — {today}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{len(enriched_leads)} leads enriched"}],
        },
        {"type": "divider"},
    ]

    for item in enriched_leads:
        lead = item["lead"]
        enrichment = item["enrichment"]
        synthesis = item["synthesis"]

        # Summary line
        summary = lead.get("summary", "No summary")
        sender = lead.get("sender_username", "Unknown")
        group = lead.get("group_name", "")
        text_lines = [f"*{summary}*", f"From: {group} | @{sender}"]

        # Metrics line
        metrics = []
        tw = enrichment.get("twitter")
        if tw:
            metrics.append(f"@{tw['username']}")
            metrics.append(f"{tw['followers']:,} followers")
        eng = enrichment.get("twitter_engagement")
        if eng:
            metrics.append(f"Avg {eng['avg_likes']} likes/tweet")
        disc = enrichment.get("discord")
        if disc:
            metrics.append(f"Discord: {disc['member_count']:,} members")
        web = enrichment.get("website")
        if web:
            domain = urlparse(web.get("url", "")).netloc.replace("www.", "")
            if domain:
                metrics.append(domain)
        if metrics:
            text_lines.append(" | ".join(metrics))

        # Links line
        links = []
        tg_link = lead.get("telegram_link", "")
        if tg_link:
            links.append(f"<{tg_link}|TG Message>")
        if tw:
            links.append(f"<https://x.com/{tw['username']}|Twitter>")
        if web:
            links.append(f"<{web['url']}|Website>")
        if links:
            text_lines.append(" | ".join(links))

        # Fit score + assessment
        score = synthesis.get("fit_score", 0)
        assessment = synthesis.get("assessment", "")
        text_lines.append(f"Fit: {score}/5 — {assessment}")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(text_lines)},
        })
        blocks.append({"type": "divider"})

    return blocks


def send_enriched_digest(enriched_leads):
    """Send the enriched digest to Slack, handling the 50-block limit."""
    if not enriched_leads:
        print("No enriched leads to send.")
        return

    blocks = format_slack_blocks(enriched_leads)
    client = WebClient(token=SLACK_BOT_TOKEN)

    # Slack allows max 50 blocks per message — split if needed
    MAX_BLOCKS = 50
    for i in range(0, len(blocks), MAX_BLOCKS):
        chunk = blocks[i:i + MAX_BLOCKS]
        try:
            # First chunk gets the header text as fallback
            fallback = f"Enriched Lead Digest — {len(enriched_leads)} leads"
            client.chat_postMessage(channel=SLACK_CHANNEL, text=fallback, blocks=chunk, unfurl_links=False, unfurl_media=False)
        except SlackApiError as e:
            print(f"❌ Slack error: {e.response['error']}")
            raise

    print(f"✅ Enriched digest sent to Slack ({len(enriched_leads)} leads)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _start_time
    _start_time = time.time()

    # Load leads
    try:
        with open("leads.json") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("No leads.json found — nothing to enrich.")
        return
    except json.JSONDecodeError as e:
        print(f"❌ Invalid leads.json: {e}")
        return

    leads = data.get("leads", [])
    if not leads:
        print("No leads found in leads.json.")
        return

    print(f"🔬 Enriching {len(leads)} leads...")
    enriched_leads = []

    for i, lead in enumerate(leads):
        if not time_remaining():
            print(f"⏰ Time ceiling reached after {i} leads — sending partial results.")
            break

        lead_id = lead.get("id", f"lead_{i}")
        print(f"\n  [{i+1}/{len(leads)}] {lead_id}")

        try:
            enrichment = enrich_lead(lead)
            synthesis = synthesize_lead(lead, enrichment)
            enriched_leads.append({
                "lead": lead,
                "enrichment": enrichment,
                "synthesis": synthesis,
            })
            score = synthesis.get("fit_score", "?")
            print(f"    ✓ Fit: {score}/5")
        except Exception as e:
            print(f"    ⚠️  Error enriching {lead_id}: {e}")
            enriched_leads.append({
                "lead": lead,
                "enrichment": {},
                "synthesis": {
                    "fit_score": 0,
                    "assessment": "Enrichment failed — manual review needed.",
                    "key_signals": [],
                },
            })

    # Write enriched data
    with open("enriched_leads.json", "w") as f:
        json.dump({
            "scan_date": data.get("scan_date", ""),
            "enriched_at": datetime.now(timezone.utc).isoformat(),
            "leads": enriched_leads,
        }, f, indent=2)
    print(f"\n📝 Wrote {len(enriched_leads)} enriched leads to enriched_leads.json")

    # Send to Slack
    send_enriched_digest(enriched_leads)

    elapsed = time.time() - _start_time
    print(f"\n⏱️  Done in {elapsed:.1f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        try:
            client = WebClient(token=SLACK_BOT_TOKEN)
            client.chat_postMessage(
                channel=SLACK_CHANNEL,
                text=f"⚠️ Lead Enricher Error:\n```{e}```",
            )
        except Exception:
            pass
        sys.exit(1)
