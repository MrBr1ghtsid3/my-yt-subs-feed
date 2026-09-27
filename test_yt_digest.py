import contextlib
import io
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yt_digest as yd

CH_A = "UC" + "A" * 22
CH_B = "UC" + "B" * 22
CH_C = "UC" + "C" * 22  # always fails


def entry(vid, title, published, desc, author, shorts=False):
    href = f"https://www.youtube.com/{'shorts/' + vid if shorts else 'watch?v=' + vid}"
    return f"""
 <entry>
  <id>yt:video:{vid}</id>
  <yt:videoId>{vid}</yt:videoId>
  <yt:channelId>x</yt:channelId>
  <title>{title}</title>
  <link rel="alternate" href="{href}"/>
  <author><name>{author}</name><uri>https://www.youtube.com/channel/x</uri></author>
  <published>{published}</published>
  <updated>{published}</updated>
  <media:group>
   <media:title>{title}</media:title>
   <media:content url="https://www.youtube.com/v/{vid}?version=3" type="application/x-shockwave-flash" width="640" height="390"/>
   <media:thumbnail url="https://i1.ytimg.com/vi/{vid}/hqdefault.jpg" width="480" height="360"/>
   <media:description>{desc}</media:description>
   <media:community>
    <media:starRating count="100" average="5.00" min="1" max="5"/>
    <media:statistics views="12345"/>
   </media:community>
  </media:group>
 </entry>"""


def feed(author, entries):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <link rel="self" href="http://www.youtube.com/feeds/videos.xml?playlist_id=UULF"/>
 <id>yt:playlist:UULF</id>
 <title>Videos</title>
 <author><name>{author}</name></author>
 <published>2015-01-01T00:00:00+00:00</published>
 {''.join(entries)}
</feed>""".encode()


NOISY = """In this video we walk the old Roman road along the Danube and look at why the fortress at Durostorum was placed where it was. We also compare two competing theories about the river's course.

Thanks to Squarespace for sponsoring this video! Use code DANUBE for 10% off.
🔗 Links:
https://example.com/source-1
https://example.com/source-2
00:00 Intro
03:12 The road
11:40 The fortress
Follow me on Instagram: https://instagram.com/someone
#history #danube #rome
"""

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


class FakeYouTube:
    def __init__(self):
        self.calls = []
        self.feeds = {}

    def set(self, channel_id, xml, playlist=True):
        param = ("playlist_id", "UULF" + channel_id[2:]) if playlist else ("channel_id", channel_id)
        self.feeds[yd.FEED_URL.format(param=param[0], value=param[1])] = xml

    def __call__(self, url):
        self.calls.append(url)
        if CH_C[2:] in url:
            raise yd.FetchError("HTTP 500")
        if url in self.feeds:
            return self.feeds[url]
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class SummariseTests(unittest.TestCase):
    def test_strips_boilerplate(self):
        s = yd.summarise(NOISY, 280)
        self.assertTrue(s.startswith("In this video we walk the old Roman road"))
        for junk in ("http", "Squarespace", "00:00", "#history", "Instagram", "Links"):
            self.assertNotIn(junk, s)
        self.assertLessEqual(len(s), 281)

    def test_truncates_at_sentence(self):
        s = yd.summarise(NOISY, 150)
        self.assertTrue(s.endswith("was."), s)

    def test_truncates_at_word_with_ellipsis(self):
        s = yd.summarise("word " * 100, 50)
        self.assertTrue(s.endswith("…"))
        self.assertLessEqual(len(s), 51)

    def test_links_only(self):
        self.assertEqual(yd.summarise("https://a.com\nhttps://b.com\n#tag", 280), "(No description.)")
        self.assertEqual(yd.summarise("", 280), "(No description.)")

    def test_inline_url_removed_but_sentence_kept(self):
        s = yd.summarise("The full paper is at https://arxiv.org/abs/1234 and worth reading.", 280)
        self.assertEqual(s, "The full paper is at and worth reading.")

    def test_bulgarian_text_survives(self):
        s = yd.summarise("Разходка из Тутракан край Дунава. 🙂\nАбонирайте се за още!", 280)
        self.assertEqual(s, "Разходка из Тутракан край Дунава.")


class ParseTests(unittest.TestCase):
    def test_parse_and_filter_shorts(self):
        xml = feed("Rome on the Danube", [
            entry("v1", "Long video", iso(5), NOISY, "Rome on the Danube"),
            entry("s1", "A short", iso(4), "short", "Rome on the Danube", shorts=True),
        ])
        vids = yd.parse_feed(xml, yd.Channel(CH_A, "old name"), dict(yd.DEFAULTS))
        self.assertEqual([v.id for v in vids], ["v1"])
        v = vids[0]
        self.assertEqual(v.channel, "Rome on the Danube")  # feed name beats stale file name
        self.assertEqual(v.url, "https://www.youtube.com/watch?v=v1")
        self.assertTrue(v.summary.startswith("In this video"))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "channels.txt").write_text(
            f"# comment\n{CH_A}  # Channel A\n{CH_B}  # Channel B\n{CH_C}  # Broken\nnot-an-id\n"
        )
        self.cfg = dict(yd.DEFAULTS)
        self.yt = FakeYouTube()

    def tearDown(self):
        self.tmp.cleanup()

    def run_at(self, now):
        rc = yd.run(self.cfg, self.base, get=self.yt, now=now)
        return rc, (self.base / "output/digest.txt").read_text(encoding="utf-8")

    def page(self):
        return (self.base / "output/index.html").read_text(encoding="utf-8")

    def test_full_cycle(self):
        # A: playlist feed with one fresh video and one old one (outside first-run window).
        self.yt.set(CH_A, feed("Channel A", [
            entry("a1", "Fresh A", iso(3), NOISY, "Channel A"),
            entry("a0", "Old A", iso(24 * 5), "old", "Channel A"),
        ]))
        # B: no UULF playlist -> fallback to channel feed, which contains a Short.
        self.yt.set(CH_B, feed("Channel B", [
            entry("b1", "Fresh B &amp; more", iso(10), "Plain description.", "Channel B"),
            entry("bs", "Short B", iso(2), "x", "Channel B", shorts=True),
        ]), playlist=False)

        rc, body = self.run_at(NOW)
        self.assertEqual(rc, 0)
        self.assertIn("2 new uploads from 2 channels", body)
        self.assertIn("Title:    Fresh A", body)
        self.assertIn("Title:    Fresh B & more", body)
        self.assertNotIn("Old A", body)
        self.assertNotIn("Short B", body)
        self.assertNotIn("ytimg", body)
        self.assertIn("Date:     Sun 27 Sep 2026, 07:00 BST", body)  # 3h before 09:00 UTC, London
        self.assertIn("Broken", body)  # failure listed
        self.assertLess(body.index("CHANNEL A"), body.index("CHANNEL B"))

        # Feed: text only, no media, contains remembered old video too.
        feed_xml = (self.base / "output/feed.xml").read_bytes()
        self.assertNotIn(b"ytimg", feed_xml)
        self.assertNotIn(b"media:", feed_xml)
        root = ET.fromstring(feed_xml)
        titles = [e.findtext(f"{{{yd.ATOM}}}title") for e in root.findall(f"{{{yd.ATOM}}}entry")]
        self.assertEqual(titles[0], "Channel A: Fresh A")
        self.assertIn("Channel A: Old A", titles)

        # Page: same items, newest first, no Shorts.
        page = self.page()
        self.assertLess(page.index("Fresh A"), page.index("Fresh B &amp; more"))
        self.assertLess(page.index("Fresh B &amp; more"), page.index("Old A"))
        self.assertNotIn("Short B", page)

        # Second run, same feeds plus one new upload -> only the new one is in the digest.
        later = NOW + timedelta(days=1)
        self.yt.set(CH_A, feed("Channel A", [
            entry("a2", "Next day A", (later - timedelta(hours=1)).isoformat(), "New one.", "Channel A"),
            entry("a1", "Fresh A", iso(3), NOISY, "Channel A"),
        ]))
        _, body = self.run_at(later)
        self.assertIn("1 new upload from 1 channel", body)
        self.assertIn("Next day A", body)
        self.assertNotIn("Title:    Fresh A", body)
        self.assertLess(self.page().index("Next day A"), self.page().index("Fresh A"))

        # Third run, nothing new.
        _, body = self.run_at(later + timedelta(days=1))
        self.assertIn("No new uploads.", body)

    def test_missed_days_are_caught_up(self):
        self.yt.set(CH_A, feed("Channel A", []))
        self.yt.set(CH_B, feed("Channel B", []))
        self.run_at(NOW)
        # Runner down for four days; a video from 3.5 days ago must still arrive.
        later = NOW + timedelta(days=4)
        self.yt.set(CH_A, feed("Channel A", [
            entry("gap", "Posted during outage", (later - timedelta(hours=84)).isoformat(), "x", "Channel A"),
        ]))
        _, body = self.run_at(later)
        self.assertIn("Posted during outage", body)

    def test_all_failed_leaves_state(self):
        (self.base / "channels.txt").write_text(f"{CH_C}\n")
        rc = yd.run(self.cfg, self.base, get=self.yt, now=NOW)
        self.assertEqual(rc, 1)
        self.assertFalse((self.base / "state.json").exists())

    def test_unknown_timezone_gives_tzdata_hint(self):
        self.cfg["timezone"] = "Nowhere/Invalid"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = yd.run(self.cfg, self.base, get=self.yt, now=NOW)
        self.assertEqual(rc, 2)
        self.assertEqual(self.yt.calls, [])  # stops before fetching anything
        msg = err.getvalue().strip()
        self.assertEqual(msg.count("\n"), 0)
        self.assertIn("python -m pip install tzdata", msg)


class PageTests(unittest.TestCase):
    SITE = "https://example.github.io/yt-digest/"

    def build(self, title="Plain title", channel="Channel A", summary="A summary."):
        v = yd.Video(
            id="v1", channel_id=CH_A, channel=channel, title=title,
            url="https://www.youtube.com/watch?v=v1", published=iso(3), summary=summary,
        )
        cfg = dict(yd.DEFAULTS, site_url=self.SITE)
        return yd.build_html([v], cfg, yd.ZoneInfo("Europe/London"), NOW).decode("utf-8")

    def test_no_images_scripts_or_external_files(self):
        page = self.build().lower()
        for bad in ("<img", "<script", "<svg", "<iframe", "stylesheet", "@import", "url(", "ytimg"):
            self.assertNotIn(bad, page)

    def test_text_is_escaped(self):
        page = self.build(
            title='<script>alert("x")</script> & more',
            channel="Tom & Jerry <b>",
            summary="1 < 2 and <img src=x onerror=alert(1)>",
        )
        self.assertNotIn("<script", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; more", page)
        self.assertIn("Tom &amp; Jerry &lt;b&gt;", page)
        self.assertIn("1 &lt; 2 and &lt;img src=x onerror=alert(1)&gt;", page)

    def test_feed_link_and_autodiscovery(self):
        page = self.build()
        feed = self.SITE + "feed.xml"
        self.assertIn(f'<link rel="alternate" type="application/atom+xml" title="YouTube subscriptions (text only)" href="{feed}">', page)
        self.assertIn(f'<a href="{feed}">', page)

    def test_entry_fields(self):
        page = self.build()
        self.assertIn('<a href="https://www.youtube.com/watch?v=v1">Plain title</a>', page)
        self.assertIn("Channel A", page)
        self.assertIn("Sun 27 Sep 2026, 07:00 BST", page)
        self.assertIn("A summary.", page)

    def test_non_https_link_is_replaced(self):
        v = yd.Video("v1", CH_A, "A", "T", "javascript:alert(1)", iso(3), "s")
        page = yd.build_html([v], dict(yd.DEFAULTS), yd.ZoneInfo("UTC"), NOW).decode()
        self.assertNotIn("javascript:", page)
        self.assertIn('href="https://www.youtube.com/watch?v=v1"', page)

    def test_empty_page(self):
        page = yd.build_html([], dict(yd.DEFAULTS), yd.ZoneInfo("UTC"), NOW).decode()
        self.assertIn("No uploads yet.", page)
        self.assertIn('href="feed.xml"', page)  # relative when no site_url


class AtomTests(unittest.TestCase):
    def test_self_link(self):
        cfg = dict(yd.DEFAULTS, site_url="https://example.github.io/yt-digest")
        root = ET.fromstring(yd.build_atom([], cfg, yd.ZoneInfo("UTC"), NOW))
        link = root.find(f"{{{yd.ATOM}}}link[@rel='self']")
        self.assertEqual(link.get("href"), "https://example.github.io/yt-digest/feed.xml")

    def test_no_self_link_without_site_url(self):
        root = ET.fromstring(yd.build_atom([], dict(yd.DEFAULTS), yd.ZoneInfo("UTC"), NOW))
        self.assertIsNone(root.find(f"{{{yd.ATOM}}}link"))


class ImportTests(unittest.TestCase):
    def test_takeout_import_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "subs.csv").write_text(
                "﻿Channel Id,Channel Url,Channel Title\n"
                f"{CH_A},http://www.youtube.com/channel/{CH_A},Channel A\n"
                f"{CH_B},http://www.youtube.com/channel/{CH_B},\"Channel B, with comma\"\n"
                "\n",
                encoding="utf-8",
            )
            chans = yd.import_takeout(d / "subs.csv")
            self.assertEqual([c.name for c in chans], ["Channel A", "Channel B, with comma"])
            path = d / "channels.txt"
            self.assertEqual(yd.append_channels(path, chans), 2)
            self.assertEqual(yd.append_channels(path, chans), 0)  # idempotent
            self.assertEqual([c.id for c in yd.load_channels(path)], [CH_A, CH_B])

    def test_resolve_handle(self):
        page = (f'<html><link rel="alternate" type="application/rss+xml" title="RSS" '
                f'href="https://www.youtube.com/feeds/videos.xml?channel_id={CH_A}"></html>').encode()
        pages = {
            "https://www.youtube.com/@someone": page,
            yd.FEED_URL.format(param="channel_id", value=CH_A): feed("Someone's Channel", []),
        }
        ch = yd.resolve_channel("@someone", get=pages.__getitem__)
        self.assertEqual((ch.id, ch.name), (CH_A, "Someone's Channel"))
        ch = yd.resolve_channel(f"https://www.youtube.com/channel/{CH_A}", get=pages.__getitem__)
        self.assertEqual(ch.id, CH_A)


if __name__ == "__main__":
    unittest.main()
