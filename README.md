# xthread

Prints an X/Twitter thread, or a tweet with its full X Article, from a tweet URL as plain text or JSON. Single tweets and articles need no login. Full threads need the cookies of a throwaway account. No API key, no dependencies.

```
$ xthread https://x.com/posthog/status/2109706421919248499
1. @posthog — Mon Oct 12 18:03:02 +0000 2026
   We have built 10 different AI products at PostHog ... Here are the 38 most important lessons:
   likes=1582 retweets=98 replies=27 bookmarks=4471 quotes=53 views=229301

2. @posthog — Mon Oct 12 18:03:04 +0000 2026
   1. AI can make your product worse. It can be slow and unreliable, ...
...
40. @posthog — Mon Oct 12 18:04:56 +0000 2026
   Subscribe for more lessons like these: https://posthog.com/newsletter
```

## Setup

```bash
uv tool install xthread-cli    # or: pipx install xthread-cli
xthread https://x.com/chhddavid/status/2100537116635672690
```

## Usage

```bash
xthread <tweet-url>            # plain text (default)
xthread <tweet-url> --json
xthread <url-or-raw-id> --timeout 30
```

It accepts `https://x.com/<user>/status/<id>`, `https://twitter.com/...` or a raw numeric ID. stdout holds data only. Errors go to stderr.

## What you get

| Input | Without cookies | With cookies |
|---|---|---|
| Single tweet | The tweet and its stats | Same |
| Long post (over 280 characters) | Full text | Same |
| X Article | Full article text | Same |
| Multi-tweet thread | First tweet only | Every tweet in the thread, oldest first |
| Photos, videos, GIFs | URLs | Same |

Links are expanded from `t.co` to the real URL. In plain output the stats line shows on the first tweet only. In `--json` output every tweet is in `thread.tweets`.

## Media

xthread prints the URLs of photos, videos and GIFs. It does not download them. Use `curl -O <url>` for that.

Lines from real threads, printed under their tweet:

```
   [photo] https://pbs.twimg.com/media/HUcvAlGa8AAmyVK.jpg?name=orig
   [video 0:58] https://video.twimg.com/amplify_video/2107400232904355841/vid/avc1/1280x588/oMK57WldoEUsd-bF.mp4?tag=29
```

When the author wrote alt text, an `alt: ...` line follows.

- **Photo:** the full-size file (`?name=orig`).
- **Video and GIF:** the MP4 with the highest bitrate. X serves GIFs as MP4. The HLS playlist (`.m3u8`) is skipped.
- **JSON:** each tweet has a `media` list. Each item has `type` (`photo`, `video` or `gif`) and `url`. Videos and GIFs also have `thumbnail` and `duration_ms`. `alt_text` is set when the author wrote one.
- The `pic.x.com` link that X adds to the tweet text is removed, because the media list replaces it.
- Quoted tweets and link cards are not read yet.

## Thread walk

X only answers the thread call (`TweetDetail`) to a logged-in session. To read full threads:

1. Make a new X account. Do not use your main account. X can lock accounts that scrape.
2. Sign in to it in a normal browser.
3. Export the `x.com` cookies as a Netscape `cookies.txt`, with any cookie-export extension.
4. Save the file as `~/.config/xthread/cookies.txt`, or set `XTHREAD_COOKIES` to its path.
5. Run `chmod 600` on the file. It holds a login token. Do not commit it or share it.

xthread uses only `auth_token` and `ct0` from the file. It follows the "show more" pages and keeps the replies of the first author in a chain. Replies from other users and promoted tweets are skipped. If the author replied twice to one tweet, only the earlier branch is followed.

Checked on 2026-10-17: a 40-tweet thread and a 76-tweet thread. Both gave the same tweet counts as twitter-thread.com.

If X rejects the cookies (expired, or the account is locked), xthread exits with code 5 and prints the reason. It never prints a partial thread as if it were complete.

## Exit codes

| Code | Meaning |
|----:|---------|
| 0 | Success. A thread or an article was printed. |
| 1 | Bad or missing arguments. |
| 2 | One tweet, no article. It is still printed. This is also the result for a thread when no cookies file exists. |
| 3 | Tweet not found. |
| 4 | Network error or timeout. |
| 5 | X rejected the request: rate limit, bad cookies, or an unexpected response. |

## How it works

Single tweets and articles use X's guest API. The public web client contains a fixed bearer token. xthread sends it to `guest/activate.json` for a guest token. It then calls `TweetResultByRestId` with `withArticlePlainText=true`, which returns the tweet and any article body in one request.

Threads use `TweetDetail` with your session cookies. The query ID of that call changes every few weeks. xthread has one built in. When X answers 400 or 404, it reads the current ID from the `main.js` file that x.com serves, and retries once.

## Limits

- This is a reverse-engineered use of X's web API. It is not an official API. X can change it at any time, and then xthread breaks until it is updated.
- Public tweets only. Private accounts and deleted tweets are not available.
- Use light, occasional reads. X rate-limits both the guest path and the session. A 429 gives exit code 5.
- Thread reading depends on one throwaway account. If X locks it, make a new one and export new cookies.

## Development

```bash
git clone https://github.com/ekinertac/xthread && cd xthread
python3 -m unittest discover -s tests
```

The tests need no network and no account. The fixture is a saved `TweetDetail` response with public tweets. Python 3.9 or newer.

## License

MIT
