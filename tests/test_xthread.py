"""Tests for the session-based thread walk in xthread.py.

Run from the repo root:  python3 -m unittest discover -s tests

fixtures/tweetdetail_posthog.json is a real TweetDetail response (public tweets, no
cookies in it): a 40-tweet thread by @posthog, one promoted tweet from another author,
and replies from other users. The pagination and query-id paths use small hand-built
pages, because a real second page would make the fixture large.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import xthread as X  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "tweetdetail_posthog.json")
FOCAL = "2109706421919248499"
PROMOTED = "2108522200919949403"


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def raw_tweet(tid, uid="1", parent=None, text="t"):
    legacy = {"id_str": tid, "user_id_str": uid, "full_text": text}
    if parent:
        legacy["in_reply_to_status_id_str"] = parent
    return {"rest_id": tid, "legacy": legacy}


def item(tid, entry_prefix="conversationthread-1-tweet-", **kw):
    return {"entryId": entry_prefix + tid, "item": {"itemContent": {
        "itemType": "TimelineTweet", "tweet_results": {"result": raw_tweet(tid, **kw)}}}}


def show_more(value):
    return {"entryId": "conversationthread-1-cursor-showmore-1", "item": {"itemContent": {
        "itemType": "TimelineTimelineCursor", "cursorType": "ShowMore", "value": value}}}


def first_page(focal, module_items):
    return {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [
            {"entryId": "tweet-" + focal, "content": {
                "entryType": "TimelineTimelineItem", "itemContent": {
                    "tweet_results": {"result": raw_tweet(focal)}}}},
            {"entryId": "conversationthread-1", "content": {
                "entryType": "TimelineTimelineModule", "items": module_items}},
        ]}]}}}


def next_page(module_items):
    return {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
        {"type": "TimelineAddToModule", "moduleItems": module_items}]}}}


class LoadSession(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_reads_tokens_including_httponly_prefix(self):
        path = self.write(
            "# Netscape HTTP Cookie File\n"
            ".x.com\tTRUE\t/\tTRUE\t0\tauth_token\tAAA\n"
            "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t0\tct0\tBBB\n")
        self.assertEqual(X.load_session(path), {"auth_token": "AAA", "ct0": "BBB"})

    def test_missing_file_or_token_gives_none(self):
        self.assertIsNone(X.load_session("/nonexistent/cookies.txt"))
        path = self.write(".x.com\tTRUE\t/\tTRUE\t0\tct0\tBBB\n")
        self.assertIsNone(X.load_session(path))

    def test_env_var_overrides_default_path(self):
        path = self.write(".x.com\tTRUE\t/\tTRUE\t0\tauth_token\tA\n.x.com\tTRUE\t/\tTRUE\t0\tct0\tB\n")
        with mock.patch.dict(os.environ, {"XTHREAD_COOKIES": path}):
            self.assertEqual(X.load_session(), {"auth_token": "A", "ct0": "B"})


class ParseDetail(unittest.TestCase):
    def test_real_response_skips_promoted_and_returns_show_more(self):
        # 40 tweets do not fit one page, so the first page ends with a ShowMore cursor
        tweets, cursor = X.parse_detail(load_fixture())
        self.assertIn(FOCAL, tweets)
        self.assertNotIn(PROMOTED, tweets)
        self.assertTrue(cursor)

    def test_show_more_cursor_is_returned_not_treated_as_tweet(self):
        tweets, cursor = X.parse_detail(first_page("10", [item("11", parent="10"), show_more("CUR")]))
        self.assertEqual(sorted(tweets), ["10", "11"])
        self.assertEqual(cursor, "CUR")

    def test_add_to_module_page(self):
        tweets, cursor = X.parse_detail(next_page([item("12", parent="11"), show_more("CUR2")]))
        self.assertEqual(list(tweets), ["12"])
        self.assertEqual(cursor, "CUR2")

    def test_unexpected_shape_raises(self):
        with self.assertRaises(X.AuthRateError):
            X.parse_detail({"data": {}})


class ThreadChain(unittest.TestCase):
    def test_real_response_chain_is_one_author_in_order(self):
        tweets, _ = X.parse_detail(load_fixture())
        chain = X.thread_chain(tweets, FOCAL)
        ids = [t["legacy"]["id_str"] for t in chain]
        self.assertEqual(ids[0], FOCAL)
        self.assertEqual(ids, sorted(ids, key=int))
        self.assertEqual({t["legacy"]["user_id_str"] for t in chain}, {chain[0]["legacy"]["user_id_str"]})
        self.assertGreaterEqual(len(chain), 20)  # first page only; the rest sits behind ShowMore

    def test_ignores_other_authors_replies(self):
        tweets = {t["legacy"]["id_str"]: t for t in [
            raw_tweet("1"), raw_tweet("2", parent="1"), raw_tweet("3", uid="9", parent="1"),
            raw_tweet("4", parent="2")]}
        self.assertEqual([t["legacy"]["id_str"] for t in X.thread_chain(tweets, "1")], ["1", "2", "4"])

    def test_focal_in_the_middle_walks_up_and_down(self):
        tweets = {t["legacy"]["id_str"]: t for t in [
            raw_tweet("1"), raw_tweet("2", parent="1"), raw_tweet("3", parent="2")]}
        self.assertEqual([t["legacy"]["id_str"] for t in X.thread_chain(tweets, "2")], ["1", "2", "3"])

    def test_missing_focal_gives_empty_chain(self):
        self.assertEqual(X.thread_chain({}, "1"), [])


class WalkThread(unittest.TestCase):
    def test_follows_show_more_across_pages(self):
        pages = [first_page("1", [item("2", parent="1"), show_more("C1")]),
                 next_page([item("3", parent="2"), show_more("C2")]),
                 next_page([item("4", parent="3")])]
        calls = []

        def fake(session, qid, tid, cursor=None, timeout=30):
            calls.append(cursor)
            return pages[len(calls) - 1]

        with mock.patch.object(X, "gql_detail", fake):
            chain = X.walk_thread({"auth_token": "a", "ct0": "c"}, "1")
        self.assertEqual([t["legacy"]["id_str"] for t in chain], ["1", "2", "3", "4"])
        self.assertEqual(calls, [None, "C1", "C2"])

    def test_stale_query_id_is_refreshed_once(self):
        used = []

        def fake(session, qid, tid, cursor=None, timeout=30):
            used.append(qid)
            if qid == X.QUERY_ID_DETAIL:
                raise X.QueryIdError("404")
            return first_page("1", [])

        with mock.patch.object(X, "gql_detail", fake), \
                mock.patch.object(X, "discover_detail_qid", return_value="NEWQID"):
            chain = X.walk_thread({"auth_token": "a", "ct0": "c"}, "1")
        self.assertEqual(used, [X.QUERY_ID_DETAIL, "NEWQID"])
        self.assertEqual(len(chain), 1)

    def test_still_stale_after_refresh_raises(self):
        def fake(*a, **k):
            raise X.QueryIdError("404")

        with mock.patch.object(X, "gql_detail", fake), \
                mock.patch.object(X, "discover_detail_qid", return_value="NEWQID"):
            with self.assertRaises(X.AuthRateError):
                X.walk_thread({"auth_token": "a", "ct0": "c"}, "1")


class FlattenAndPlain(unittest.TestCase):
    def test_note_tweet_text_beats_truncated_legacy_and_links_expand(self):
        result = {"rest_id": "1",
                  "legacy": {"id_str": "1", "full_text": "short…"},
                  "note_tweet": {"note_tweet_results": {"result": {
                      "text": "the whole long post https://t.co/abc",
                      "entity_set": {"urls": [{"url": "https://t.co/abc",
                                               "expanded_url": "https://example.com/x"}]}}}}}
        self.assertEqual(X.flatten(result)["text"], "the whole long post https://example.com/x")

    def test_legacy_text_used_without_note_tweet(self):
        result = {"rest_id": "1", "legacy": {"id_str": "1", "full_text": "hi"}}
        self.assertEqual(X.flatten(result)["text"], "hi")

    def test_to_plain_numbers_tweets_and_puts_stats_on_first_only(self):
        mk = lambda n: {"author": {"screen_name": "a"}, "created_at": "d", "text": f"text{n}",
                        "stats": {}, "article": None}
        out = X.to_plain([mk(1), mk(2)])
        self.assertIn("1. @a — d", out)
        self.assertIn("2. @a — d", out)
        self.assertEqual(out.count("likes="), 1)


if __name__ == "__main__":
    unittest.main()
