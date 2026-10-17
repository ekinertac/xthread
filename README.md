# xthread

Prints an X/Twitter thread, or a tweet with its full X Article, from a tweet URL as plain text or JSON. Single tweets and articles need no login. Full threads need the cookies of a throwaway account. No API key, no dependencies.

## Setup

```bash
uv tool install xthread-cli    # or: pipx install xthread-cli
xthread https://x.com/chhddavid/status/2100537116635672690
```

## Usage

```bash
xthread <tweet-url>            # --plain (default)
xthread <tweet-url> --json
xthread <url-or-raw-id> --timeout 30
```

Accepts `https://x.com/<author>/<status>` / `https://twitter.com/...` / a raw
numeric tweet ID.

- **stdout is data only** — human-readable text (`--plain`, default) or pure
  JSON (`--json`). No progress chatter.
- `--timeout N` sets a per-request timeout in seconds (default 30).

## The mechanism (3 sentences)

1. X's public web client ships a **static Bearer token**; posting it to
   `POST https://api.x.com/1.1/guest/activate.json` returns a throwaway **guest
   token**, requiring no API key and no login.
2. That guest token calls X's internal web GraphQL op **`TweetResultByRestId`**
   (which X's own web app allows to anonymous guests), returning the focal
   tweet's text, author, counts and dates, plus — with the
   `withArticlePlainText=true` field toggle — the **full body of any linked X
   Article** as `plain_text`.
3. That single guest call is the entire keyless path: xthread reads the tweet
   and its article straight from X's own web API, without a logged-in session
   or a headless browser.

## What it returns for the test tweet

For `https://x.com/chhddavid/status/2100537116635672690` (keyless, live):

- **Author:** `@chhddavid` (user_id 2732582439)
- **Tweet text:** `https://t.co/iFzB9PR6Ho` (an X Article link)
- **Stats:** likes 1255, retweets 72, replies 26, bookmarks ~6967, quotes 18,
  views ~909k
- **Created:** `Thu Sep 17 10:47:29 +0000 2026`
- **Article title:** *I manually studied 30,600 iOS apps making $20k-100k/mo+,
  the recipe is stupidly easy.*
- **Article body:** 21,474 characters / 424 content blocks — the complete
  article, returned in one keyless call.

So the "thread" for this tweet is the **article**: the tweet is a single post
whose content is an X Article, and xthread returns that full article text.

## Thread walk (needs a throwaway X account)

1. Make a new X account. Do not use your main one: X can lock accounts that scrape.
2. Sign in with a normal browser and export `x.com` cookies as Netscape `cookies.txt`.
3. Save the file as `~/.config/xthread/cookies.txt` (mode 600) or set `$XTHREAD_COOKIES`.
   Only `auth_token` and `ct0` are used. Never commit this file.

xthread then calls `TweetDetail`, follows the `ShowMore` cursors, and prints the
same-author reply chain, oldest first. Checked 2026-10-17: 40 tweets (@posthog) and
76 tweets (a Turkish football thread), the same counts twitter-thread.com shows.
If X rejects the session (expired cookies, locked account) xthread exits 5. It does
not print a partial thread. The `TweetDetail` query id rotates, so xthread reads the
current one from X's `main.js` when the built-in one fails.

## Exit codes

| code | meaning |
|----:|---------|
| 0 | success — tweet (and article body, when present) fetched and printed |
| 1 | usage error (bad / missing args) |
| 2 | not a thread — standalone single tweet, no article; still printed |
| 3 | tweet not found |
| 4 | network / timeout error |
| 5 | auth / rate-limit / unexpected error |

## Limits (read these before building on it)

1. **A multi-tweet thread cannot be walked keylessly.** `TweetDetail` only
   answers a logged-in session. Without a session file xthread returns the
   focal tweet plus any linked article and says so in the JSON `thread.note`.
   With a session file it walks the whole thread (see "Thread walk" below).
2. **Rate limits are tight.** The guest path is throttled; the token and
   endpoints are intended for light, occasional use. Aggressive use returns
   429s (exit 5).
3. **The tokens/IDs are scraped, not official.** The static Bearer token and the
   GraphQL query IDs come from X's public web-client JavaScript. X can rotate
   them at any time, breaking the keyless path. This is a reverse-engineered
   integration against a non-public endpoint, not a stable supported API.
4. **Public content only.** Private, age-gated, or behind-paywall tweets are
   not reachable this way.

## Files

- `xthread.py` — the CLI (stdlib only: `argparse`, `urllib`, `json`, `os`, `re`,
  `textwrap`, `secrets`, `sys`).
- `tests/test_xthread.py` — `python3 -m unittest discover -s tests`. The fixture is
  a real `TweetDetail` response.
