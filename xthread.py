#!/usr/bin/env python3
"""xthread — read an X/Twitter thread (or single tweet / article) from any tweet URL.

No API key. Stdlib only (Python 3). The focal tweet and any X Article are keyless;
the multi-tweet thread walk needs the cookies of a THROWAWAY X account (see below).

How the keyless part works:
  1. POST https://api.x.com/1.1/guest/activate.json with X's static web-app Bearer
     token -> guest_token.  (That Bearer token ships inside X's public web client JS.)
  2. GET  https://api.x.com/graphql/<qid>/TweetResultByRestId with the guest token.
     This is X's internal web GraphQL op that is allowed to guests. With the full
     feature set and the field toggle withArticlePlainText=true it returns the
     focal tweet (text, counts, dates, author) plus, when the tweet links to an
     X Article, the article's full body as plain_text.

Exit codes:
  0  success — tweet (and article body, when present) fetched and printed
  2  not a thread — standalone single tweet, no article; still printed
  3  tweet not found
  4  network / timeout error
  5  auth / rate-limit / unexpected error
  1  usage error (bad args)

Thread walk (needs a session):
  X's TweetDetail op returns the conversation, but only to a logged-in session.
  If a Netscape cookies.txt with auth_token + ct0 exists (path in $XTHREAD_COOKIES,
  default ~/.config/xthread/cookies.txt), xthread calls TweetDetail with it, follows
  the ShowMore cursors, and keeps the focal author's reply chain. Without the file
  it prints the focal tweet only (old behaviour). If X rejects the session (expired
  cookies, locked account) it exits 5 instead of silently printing a partial thread.
  Use a throwaway account: X can lock accounts that scrape. Never commit the file.
  The TweetDetail query id rotates; xthread reads the current one from X's main.js
  when the built-in one stops working.
"""
import argparse
import json
import os
import re
import secrets
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request

BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")  # static web-app Bearer token
API_ROOT = "https://api.x.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
QUERY_ID_TWEET = "f2sagi1jweVHFkTUIHzmMQ"  # TweetResultByRestId (guest-allowed)
QUERY_ID_DETAIL = "3BlMtudy4N-d78ch7zTLZQ"  # TweetDetail (session only); rotates
SESSION_PATH = os.path.expanduser("~/.config/xthread/cookies.txt")
# shortcut: each ShowMore page holds ~30 tweets, so 40 pages ~ 1.2k tweets; raise if needed
MAX_PAGES = 40
FEATURES = [
    "rweb_video_screen_enabled",
    "profile_label_improvements_pcf_label_in_post_enabled",
    "responsive_web_profile_redirect_enabled",
    "rweb_tipjar_consumption_enabled",
    "verified_phone_label_enabled",
    "creator_subscriptions_tweet_preview_api_enabled",
    "responsive_web_graphql_timeline_navigation_enabled",
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled",
    "premium_content_api_read_enabled",
    "communities_web_enable_tweet_community_results_fetch",
    "c9s_tweet_anatomy_moderator_badge_enabled",
    "responsive_web_grok_analyze_button_fetch_trends_enabled",
    "responsive_web_grok_analyze_post_followups_enabled",
    "responsive_web_jetfuel_frame",
    "responsive_web_grok_share_attachment_enabled",
    "responsive_web_grok_annotations_enabled",
    "articles_preview_enabled",
    "responsive_web_edit_tweet_api_enabled",
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled",
    "view_counts_everywhere_api_enabled",
    "longform_notetweets_consumption_enabled",
    "responsive_web_twitter_article_tweet_consumption_enabled",
    "content_disclosure_indicator_enabled",
    "content_disclosure_ai_generated_indicator_enabled",
    "responsive_web_grok_show_grok_translated_post",
    "responsive_web_grok_analysis_button_from_backend",
    "post_ctas_fetch_enabled",
    "freedom_of_speech_not_reach_fetch_enabled",
    "standardized_nudges_misinfo",
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled",
    "longform_notetweets_rich_text_read_enabled",
    "longform_notetweets_inline_media_enabled",
    "responsive_web_grok_image_annotation_enabled",
    "responsive_web_grok_imagine_annotation_enabled",
    "responsive_web_grok_community_note_auto_translation_is_enabled",
    "responsive_web_enhance_cards_enabled",
    "tweet_awards_web_tipping_enabled",
]

EXIT_SUCCESS, EXIT_NOT_THREAD, EXIT_NOT_FOUND, EXIT_NET, EXIT_ERR, EXIT_USE = (
    0, 2, 3, 4, 5, 1)


def _do(req, timeout=30):
    """GET/POST. Returns (status, body_bytes). Raises NetError on transport errors."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise NetError(f"network error: {e.reason}") from e
    except (TimeoutError, ConnectionError) as e:
        raise NetError(f"timeout/connection error: {e}") from e


class NetError(Exception):
    pass


class NotFoundError(Exception):
    pass


class AuthRateError(Exception):
    pass


def guest_token(timeout=15):
    req = urllib.request.Request(
        f"{API_ROOT}/1.1/guest/activate.json", method="POST", data=b"",
        headers={"Authorization": f"Bearer {BEARER}", "User-Agent": UA})
    status, body = _do(req, timeout=timeout)
    if status != 200:
        text = body.decode(errors="replace")[:150]
        if status == 429:
            raise AuthRateError(f"rate-limited activating guest token (429): {text}")
        if status in (401, 403):
            raise AuthRateError(f"guest token rejected ({status}): {text}")
        raise AuthRateError(f"guest/activate failed ({status}): {text}")
    data = json.loads(body.decode(errors="replace"))
    token = data.get("guest_token")
    if token is None:
        raise AuthRateError(f"activate response had no guest_token: {data}")
    return token


def guest_headers(gt, csrf):
    return {
        "Authorization": f"Bearer {BEARER}",
        "User-Agent": UA,
        "x-guest-token": str(gt),
        "x-csrf-token": csrf,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "Cookie": (f"guest_id=v1%3A{gt}; guest_id_ads=v1%3A{gt}; "
                   f"guest_id_marketing=v1%3A{gt}; ct0={csrf}"),
    }


def gql_tweet(gt, tweet_id, timeout=30):
    """Keyless focal-tweet lookup via TweetResultByRestId (guest-allowed).

    withArticlePlainText=true makes the full article body come back as
    article.article_results.result.plain_text, with no extra request.
    """
    variables = {"tweetId": tweet_id,
                 "withCommunity": False,
                 "includePromotedContent": False,
                 "withVoice": False}
    field_toggles = {"withArticleRichContentState": True,
                     "withArticlePlainText": True,
                     "withGrokAnalyze": False,
                     "withDisallowedReplyControls": False}
    url = (f"{API_ROOT}/graphql/{QUERY_ID_TWEET}/TweetResultByRestId?variables="
           + urllib.parse.quote(json.dumps(variables))
           + f"&features={urllib.parse.quote(json.dumps({k: True for k in FEATURES}))}"
           + f"&fieldToggles={urllib.parse.quote(json.dumps(field_toggles))}")
    status, body = _do(urllib.request.Request(
        url, headers=guest_headers(gt, secrets.token_hex(16))), timeout=timeout)
    if status == 404:
        raise NotFoundError("TweetResultByRestId returned 404 (tweet not found / not public)")
    if status in (401, 403, 429):
        raise AuthRateError(f"GraphQL rejected ({status}): {body.decode(errors='replace')[:150]}")
    if status != 200:
        raise AuthRateError(f"GraphQL failed ({status}): {body.decode(errors='replace')[:150]}")
    data = json.loads(body.decode(errors="replace"))
    try:
        return data["data"]["tweetResult"]["result"]
    except (KeyError, TypeError):
        raise NotFoundError(f"tweetResult missing from response: {list(data.get('data', {}))}")


class QueryIdError(Exception):
    """TweetDetail answered 400/404: the built-in query id is probably stale."""


def load_session(path=None):
    """Read auth_token + ct0 from a Netscape cookies.txt. None if file or cookies missing."""
    path = path or os.environ.get("XTHREAD_COOKIES") or SESSION_PATH
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    jar = {}
    for line in lines:
        line = line.removeprefix("#HttpOnly_")  # Chrome exports HttpOnly cookies this way
        parts = line.split("\t")
        if len(parts) >= 7 and not line.startswith("#"):
            jar[parts[5]] = parts[6]
    if jar.get("auth_token") and jar.get("ct0"):
        return {"auth_token": jar["auth_token"], "ct0": jar["ct0"]}
    return None


def session_headers(session):
    return {
        "Authorization": f"Bearer {BEARER}",
        "User-Agent": UA,
        "x-csrf-token": session["ct0"],
        "Cookie": f"auth_token={session['auth_token']}; ct0={session['ct0']}",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }


def discover_detail_qid(session, timeout=30):
    """Read the current TweetDetail query id from the main.js bundle that x.com serves."""
    _, home = _do(urllib.request.Request("https://x.com/", headers=session_headers(session)),
                  timeout=timeout)
    js = re.search(r'https://abs\.twimg\.com/responsive-web/client-web[^"\']*?/main\.[0-9a-z]+\.js',
                   home.decode(errors="replace"))
    if not js:
        raise AuthRateError("could not find X's main.js (session expired or page changed)")
    _, bundle = _do(urllib.request.Request(js.group(0), headers={"User-Agent": UA}),
                    timeout=timeout)
    m = re.search(r'queryId:"([^"]+)",operationName:"TweetDetail"', bundle.decode(errors="replace"))
    if not m:
        raise AuthRateError("TweetDetail query id not found in main.js")
    return m.group(1)


def gql_detail(session, qid, tweet_id, cursor=None, timeout=30):
    variables = {"focalTweetId": tweet_id, "with_rux_injections": False,
                 "rankingMode": "Relevance", "includePromotedContent": False,
                 "withCommunity": True, "withQuickPromoteEligibilityTweetFields": False,
                 "withBirdwatchNotes": False, "withVoice": True}
    if cursor:
        variables["cursor"] = cursor
    url = (f"https://x.com/i/api/graphql/{qid}/TweetDetail?variables="
           + urllib.parse.quote(json.dumps(variables))
           + f"&features={urllib.parse.quote(json.dumps({k: True for k in FEATURES}))}"
           + "&fieldToggles=" + urllib.parse.quote(json.dumps(
               {"withArticleRichContentState": True, "withArticlePlainText": True,
                "withGrokAnalyze": False, "withDisallowedReplyControls": False})))
    status, body = _do(urllib.request.Request(url, headers=session_headers(session)),
                       timeout=timeout)
    if status in (400, 404):
        raise QueryIdError(f"TweetDetail returned {status}")
    if status in (401, 403, 429):
        raise AuthRateError(f"session rejected ({status}); re-export the cookies or use "
                            f"another account: {body.decode(errors='replace')[:150]}")
    if status != 200:
        raise AuthRateError(f"TweetDetail failed ({status}): {body.decode(errors='replace')[:150]}")
    return json.loads(body.decode(errors="replace"))


def parse_detail(data):
    """One TweetDetail page -> ({tweet_id: raw result}, ShowMore cursor or None).

    The first page puts the thread in a TimelineTimelineModule inside TimelineAddEntries;
    later pages come as TimelineAddToModule.moduleItems. Promoted entries are skipped.
    """
    try:
        instructions = data["data"]["threaded_conversation_with_injections_v2"]["instructions"]
    except (KeyError, TypeError):
        raise AuthRateError("unexpected TweetDetail response shape")
    tweets, cursor = {}, None
    for ins in instructions:
        items = [(i["entryId"], i["item"]["itemContent"]) for i in ins.get("moduleItems", [])]
        for e in ins.get("entries", []):
            c = e["content"]
            if c.get("entryType") == "TimelineTimelineModule":
                items += [(i["entryId"], i["item"]["itemContent"]) for i in c["items"]]
            elif c.get("entryType") == "TimelineTimelineItem":
                items.append((e["entryId"], c["itemContent"]))
        for entry_id, ic in items:
            if ic.get("cursorType") == "ShowMore":
                cursor = ic["value"]
                continue
            if "promoted" in entry_id:
                continue
            r = (ic.get("tweet_results") or {}).get("result") or {}
            if r.get("__typename") == "TweetWithVisibilityResults":
                r = r.get("tweet") or {}
            if r.get("legacy"):
                tweets[r["legacy"]["id_str"]] = r
    return tweets, cursor


def thread_chain(tweets, focal_id):
    """Same-author reply chain through the focal tweet, oldest first.

    shortcut: if the author replied twice to one tweet, only the earliest branch is
    followed. Fine for normal threads; revisit if a branching thread shows up.
    """
    focal = tweets.get(focal_id)
    if not focal:
        return []
    uid = focal["legacy"]["user_id_str"]
    children = {}
    for t in tweets.values():
        parent = t["legacy"].get("in_reply_to_status_id_str")
        if parent and t["legacy"]["user_id_str"] == uid:
            children.setdefault(parent, []).append(t)
    chain = [focal]
    while True:  # walk up to the thread start
        parent = tweets.get(chain[0]["legacy"].get("in_reply_to_status_id_str"))
        if not parent or parent["legacy"]["user_id_str"] != uid:
            break
        chain.insert(0, parent)
    while children.get(chain[-1]["legacy"]["id_str"]):  # then down to the end
        chain.append(min(children[chain[-1]["legacy"]["id_str"]],
                         key=lambda t: int(t["legacy"]["id_str"])))
    return chain


def walk_thread(session, tweet_id, timeout=30):
    """Fetch every TweetDetail page and return the raw results of the author's chain."""
    qid, refreshed, cursor, tweets = QUERY_ID_DETAIL, False, None, {}
    for _ in range(MAX_PAGES):
        try:
            data = gql_detail(session, qid, tweet_id, cursor, timeout)
        except QueryIdError as e:
            if refreshed:
                raise AuthRateError(f"{e} even with the query id from main.js")
            qid, refreshed = discover_detail_qid(session, timeout), True
            continue
        page, cursor = parse_detail(data)
        tweets.update(page)
        if not cursor:
            break
    return thread_chain(tweets, tweet_id)


def parse_tweet_id(url):
    if not url.startswith("http"):
        return None, url
    path = urllib.parse.urlparse(url).path
    m = re.search(r"/(?:i/)?([^/]+)/status/(\d{18,})", path)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"/status/(\d{18,})", url)
    if m:
        return None, m.group(1)
    m = re.search(r"/(\d{18,})(?:[/?#]|$)", url)
    if m:
        return None, m.group(1)
    return None, None


def extract_media(legacy):
    """Photos, videos and GIFs of one tweet as [{type, url, ...}].

    Photos get ?name=orig for the full-size file. Videos and GIFs (X serves GIFs as
    MP4) get the highest-bitrate MP4 variant; the HLS playlist is skipped because
    nothing plain can play it. `thumbnail` is the poster image.
    """
    raw = (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []
    items = []
    for m in raw:
        kind = m.get("type")
        poster = m.get("media_url_https")
        if kind == "photo":
            item = {"type": "photo", "url": f"{poster}?name=orig"}
        else:
            info = m.get("video_info") or {}
            mp4 = [v for v in info.get("variants") or []
                   if v.get("content_type") == "video/mp4" and v.get("url")]
            best = max(mp4, key=lambda v: v.get("bitrate") or 0)["url"] if mp4 else poster
            item = {"type": "gif" if kind == "animated_gif" else "video", "url": best,
                    "thumbnail": poster, "duration_ms": info.get("duration_millis")}
        if m.get("ext_alt_text"):
            item["alt_text"] = m["ext_alt_text"]
        items.append(item)
    return items


def flatten(result):
    """Normalize a raw TweetResultByRestId result into a clean dict."""
    legacy = result.get("legacy") or {}
    # author: core.user_results.result.core
    author = {"screen_name": None, "user_id": None}
    core = result.get("core") or {}
    ur = (core.get("user_results") or {}).get("result") or {}
    c = ur.get("core") or {}
    if isinstance(c, dict):
        author["screen_name"] = c.get("screen_name")
        author["user_id"] = str(ur.get("id", c.get("id"))).split(":")[-1] if ur.get("id") else None
    if not author.get("screen_name") and isinstance(legacy, dict):
        author["user_id"] = legacy.get("user_id_str")
    views = result.get("views") or {}
    try:
        views = int(views.get("count")) if isinstance(views, dict) else None
    except Exception:
        views = None
    # article (full body via plain_text, or rich blocks as fallback)
    article = None
    raw_article = result.get("article")
    if isinstance(raw_article, dict):
        res = (raw_article.get("article_results") or {}).get("result")
        if isinstance(res, dict):
            plain = res.get("plain_text") or ""
            blocks = (res.get("content_state") or {}).get("blocks") or []
            body = plain
            if not body and blocks:
                body = "".join(b.get("text", "") for b in blocks
                               if isinstance(b, dict) and isinstance(b.get("text"), str))
            article = {
                "id": res.get("id"),
                "title": res.get("title"),
                "preview_text": res.get("preview_text"),
                "body": body,
                "char_count": len(body),
                "block_count": len(blocks),
            }
    # Posts over 280 chars keep legacy.full_text cut short; the whole text is in note_tweet.
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text = note.get("text") or legacy.get("full_text") or result.get("text") or ""
    for u in (note.get("entity_set") or legacy.get("entities") or {}).get("urls") or []:
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    media = extract_media(legacy)
    for m in (legacy.get("extended_entities") or legacy.get("entities") or {}).get("media") or []:
        if m.get("url"):  # the pic.x.com link X appends; the media list replaces it
            text = text.replace(m["url"], "")
    text = text.rstrip()
    return {
        "rest_id": result.get("rest_id") or legacy.get("id_str"),
        "text": text,
        "created_at": legacy.get("created_at"),
        "lang": legacy.get("lang"),
        "possibly_sensitive": legacy.get("possibly_sensitive"),
        "is_quote": legacy.get("is_quote_status"),
        "in_reply_to_status_id": legacy.get("in_reply_to_status_id"),
        "conversation_id_str": legacy.get("conversation_id_str"),
        "author": author,
        "stats": {
            "likes": legacy.get("favorite_count"),
            "retweets": legacy.get("retweet_count"),
            "bookmarks": legacy.get("bookmark_count"),
            "quotes": legacy.get("quote_count"),
            "replies": legacy.get("reply_count"),
            "views": views,
        },
        "media": media,
        "article": article,
    }


def _media_line(m):
    label = m["type"]
    if m.get("duration_ms"):
        s = m["duration_ms"] // 1000
        label += f" {s // 60}:{s % 60:02d}"
    return f"   [{label}] {m['url']}"


def to_plain(tweets):
    """Numbered tweets, oldest first. Stats only on the first tweet to keep long threads readable."""
    lines = []
    for n, tweet in enumerate(tweets, 1):
        author = (tweet["author"] or {}).get("screen_name") or "unknown"
        lines.append(f"{n}. @{author} — {tweet['created_at'] or ''}")
        lines.append("   " + tweet["text"])
        for m in tweet.get("media") or []:
            lines.append(_media_line(m))
            if m.get("alt_text"):
                lines.append("   alt: " + " ".join(m["alt_text"].split()))
        if n == 1:
            s = tweet["stats"]
            lines.append(f"   likes={s.get('likes')} retweets={s.get('retweets')} "
                         f"replies={s.get('replies')} bookmarks={s.get('bookmarks')} "
                         f"quotes={s.get('quotes')} views={s.get('views')}")
        article_body = (tweet["article"] or {}).get("body")
        if article_body:
            title = tweet["article"].get("title")
            if title:
                lines.append("")
                lines.append(f"[article] {title}")
            lines.append("")
            for para in re.split(r"\n\s*\n", article_body):
                lines.extend(textwrap.wrap(para, 80, replace_whitespace=False) or [para])
                lines.append("")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="xthread",
        description=("Read an X/Twitter thread (or single tweet / article) from a URL. "
                     "Full threads need a cookies.txt (see README)."),
    )
    ap.add_argument("url", nargs="?", help="Tweet URL or numeric ID, e.g. https://x.com/user/status/123")
    ap.add_argument("--json", action="store_true", help="Emit JSON to stdout")
    ap.add_argument("--plain", action="store_true", help="Emit human-readable text to stdout (default)")
    ap.add_argument("--timeout", type=int, default=30,
                    help="Per-request timeout in seconds (default 30)")
    args = ap.parse_args(argv)

    if not args.url:
        print("error: a tweet URL or numeric ID is required", file=sys.stderr)
        return EXIT_USE
    if args.json and args.plain:
        print("error: --json and --plain are mutually exclusive", file=sys.stderr)
        return EXIT_USE

    author, tweet_id = parse_tweet_id(args.url)
    if not re.fullmatch(r"\d{18,}", tweet_id or ""):
        print(f"error: could not parse a tweet ID from {args.url!r}", file=sys.stderr)
        return EXIT_USE

    try:
        gt = guest_token(timeout=min(args.timeout, 30))
    except NetError as e:
        print(e, file=sys.stderr)
        return EXIT_NET
    except AuthRateError as e:
        print(e, file=sys.stderr)
        return EXIT_ERR

    try:
        result = gql_tweet(gt, tweet_id, timeout=args.timeout)
    except NetError as e:
        print(e, file=sys.stderr)
        return EXIT_NET
    except NotFoundError as e:
        print(e, file=sys.stderr)
        return EXIT_NOT_FOUND
    except AuthRateError as e:
        print(e, file=sys.stderr)
        return EXIT_ERR

    tweet = flatten(result)
    tweets, walked = [tweet], False
    session = None if tweet["article"] else load_session()  # articles are already complete
    if session:
        try:
            chain = walk_thread(session, tweet_id, timeout=args.timeout)
        except NetError as e:
            print(e, file=sys.stderr)
            return EXIT_NET
        except AuthRateError as e:
            print(e, file=sys.stderr)
            return EXIT_ERR
        if chain:
            tweets, walked = [flatten(r) for r in chain], True
    multi = len(tweets) > 1 or bool(tweet["in_reply_to_status_id"])
    output = {
        "source": "session" if walked else "keyless-guest",
        "url": args.url,
        "id": tweet["rest_id"],
        "author": tweet["author"],
        "created_at": tweet["created_at"],
        "lang": tweet["lang"],
        "possibly_sensitive": tweet["possibly_sensitive"],
        "text": tweet["text"],
        "in_reply_to_status_id": tweet["in_reply_to_status_id"],
        "conversation_id_str": tweet["conversation_id_str"],
        "stats": tweet["stats"],
        "media": tweet["media"],
        "is_article": bool(tweet["article"]),
        "is_multi_tweet_thread": multi,
        "article": tweet["article"],
        "thread": {
            "is_multi_tweet_thread": multi,
            "tweets": tweets,
            "note": None if walked or tweet["article"] else (
                "Only the focal tweet was fetched. Put a throwaway account's cookies.txt "
                f"at {SESSION_PATH} (or $XTHREAD_COOKIES) to walk the full thread."),
        },
    }

    if args.json:
        sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(to_plain(tweets))

    return EXIT_SUCCESS if (tweet["article"] or multi) else EXIT_NOT_THREAD


if __name__ == "__main__":
    sys.exit(main())
