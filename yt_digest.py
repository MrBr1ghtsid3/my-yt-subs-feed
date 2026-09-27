#!/usr/bin/env python3
"""yt-digest — a text-only digest of your YouTube subscriptions.

Each entry shows: channel name, title, date/time, and a short summary of the
description. No thumbnails, no view counts, no notifications.

Commands
  import-takeout PATH   Build/extend channels.txt from a Google Takeout subscriptions.csv
  add REF [REF ...]     Add channels by URL, @handle or UC… channel ID
  run                   Fetch feeds, write the digest and Atom feed, email if configured

Standard library only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import csv
import email.utils
import json
import os
import re
import smtplib
import ssl
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ATOM = "http://www.w3.org/2005/Atom"
NS = {
    "atom": ATOM,
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
FEED_URL = "https://www.youtube.com/feeds/videos.xml?{param}={value}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yt-digest/1.0"
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

DEFAULTS = {
    "timezone": "Europe/London",
    "channels_file": "channels.txt",
    "state_file": "state.json",
    "feed_file": "output/feed.xml",
    "digest_file": "output/digest.txt",
    "feed_title": "YouTube subscriptions (text only)",
    "summary_max_chars": 280,
    "lookback_hours": 48,
    "retain_days": 30,
    "feed_max_items": 300,
    "videos_only": True,
    "send_empty": False,
    "workers": 6,
}

# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


@dataclass
class Channel:
    id: str
    name: str


@dataclass
class Video:
    id: str
    channel_id: str
    channel: str
    title: str
    url: str
    published: str  # ISO 8601, UTC
    summary: str

    @property
    def published_dt(self) -> datetime:
        return datetime.fromisoformat(self.published)


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Config and files
# --------------------------------------------------------------------------- #


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def load_channels(path: Path) -> list[Channel]:
    channels: list[Channel] = []
    seen: set[str] = set()
    if not path.exists():
        return channels
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cid, _, comment = line.partition("#")
        cid = cid.strip()
        if not CHANNEL_ID_RE.match(cid):
            print(f"warning: {path}:{n}: not a channel ID, skipped: {cid!r}", file=sys.stderr)
            continue
        if cid in seen:
            continue
        seen.add(cid)
        channels.append(Channel(cid, comment.strip() or cid))
    return channels


def append_channels(path: Path, new: list[Channel]) -> int:
    existing = {c.id for c in load_channels(path)}
    added = [c for c in new if c.id not in existing]
    if not added:
        return 0
    with path.open("a", encoding="utf-8") as f:
        if path.stat().st_size and not path.read_text(encoding="utf-8").endswith("\n"):
            f.write("\n")
        for c in added:
            f.write(f"{c.id}  # {c.name}\n")
    return len(added)


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "last_run": None, "items": {}}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


def http_get(url: str, timeout: int = 20, retries: int = 2) -> bytes:
    """GET with small retry budget. 404 is raised immediately (it is meaningful)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-GB,en;q=0.8",
        # Skips the EU cookie-consent interstitial on channel pages.
        "Cookie": "SOCS=CAI; CONSENT=YES+1",
    }
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise FetchError(str(last))


def feed_urls(channel: Channel, videos_only: bool) -> list[str]:
    urls = []
    if videos_only:
        # "UULF" + channel ID without "UC" = the channel's long-form uploads playlist
        # (no Shorts, no past livestreams).
        urls.append(FEED_URL.format(param="playlist_id", value="UULF" + channel.id[2:]))
    urls.append(FEED_URL.format(param="channel_id", value=channel.id))
    return urls


def fetch_channel(
    channel: Channel, cfg: dict, get: Callable[[str], bytes] = http_get
) -> tuple[list[Video], str | None]:
    last_err = "no feed URL worked"
    for url in feed_urls(channel, cfg["videos_only"]):
        try:
            data = get(url)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            continue
        except FetchError as e:
            last_err = str(e)
            continue
        try:
            return parse_feed(data, channel, cfg), None
        except ET.ParseError as e:
            last_err = f"bad XML: {e}"
    return [], last_err


# --------------------------------------------------------------------------- #
# Parsing and summarising
# --------------------------------------------------------------------------- #


def parse_feed(data: bytes, channel: Channel, cfg: dict) -> list[Video]:
    root = ET.fromstring(data)
    videos = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=NS).strip()
        if not vid:
            continue
        link_el = entry.find("atom:link[@rel='alternate']", NS)
        url = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={vid}"
        if cfg["videos_only"] and "/shorts/" in url:
            continue
        published = entry.findtext("atom:published", default="", namespaces=NS)
        try:
            pub = datetime.fromisoformat(published).astimezone(timezone.utc)
        except ValueError:
            continue
        name = entry.findtext("atom:author/atom:name", default="", namespaces=NS).strip() or channel.name
        desc = entry.findtext("media:group/media:description", default="", namespaces=NS) or ""
        videos.append(
            Video(
                id=vid,
                channel_id=channel.id,
                channel=name,
                title=(entry.findtext("atom:title", default="", namespaces=NS) or "").strip(),
                url=url,
                published=pub.isoformat(),
                summary=summarise(desc, cfg["summary_max_chars"]),
            )
        )
    return videos


URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)
TIMESTAMP_RE = re.compile(r"^[\(\[]?\d{1,2}:\d{2}(:\d{2})?")
HASHTAGS_ONLY_RE = re.compile(r"^(#[\w-]+[\s,]*)+$")
HANDLE_ONLY_RE = re.compile(r"^(@[\w.-]+[\s,]*)+$")
NOISE_RE = re.compile(
    r"subscribe|patreon|sponsor|use (my )?code|promo code|discount code|coupon|affiliate"
    r"|\bmerch\b|join this channel|become a member|channel member|membership"
    r"|business (inquir|enquir|contact|email)|for business|follow (me|us)"
    r"|instagram|twitter|\bx\.com|tiktok|facebook|discord|twitch|linktree|threads\.net"
    r"|support (the|this) channel|support (me|us) on|buy me a coffee|ko-fi|paypal"
    r"|\bchapters\b|timestamps|gear (i use|used)|my gear|music (by|from|:)"
    r"|\bsoundtrack:|\bcredits:|all rights reserved|©"
    r"|абонирай|последвай|спонсор",
    re.I,
)
STRIP_CHARS = " \t-–—|•·:>*_~=+"


def _strip_symbols(s: str) -> str:
    return "".join(
        ch for ch in s if unicodedata.category(ch) not in ("So", "Sk", "Cn", "Co", "Cf")
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(". "))
    if end >= limit * 0.5:
        return cut[: end + 1]
    space = cut.rfind(" ", 0, limit)
    body = cut[:space] if space >= limit * 0.5 else text[:limit]
    return body.rstrip(" ,;:-–—") + "…"


def summarise(description: str, limit: int = 280) -> str:
    """Extractive summary: the description's opening prose, minus links,
    timestamps, hashtags, sponsor/social boilerplate and emoji."""
    paragraphs: list[list[str]] = [[]]
    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            if paragraphs[-1]:
                paragraphs.append([])
            continue
        if TIMESTAMP_RE.match(line) or HASHTAGS_ONLY_RE.match(line) or HANDLE_ONLY_RE.match(line):
            continue
        if NOISE_RE.search(line):
            continue
        clean = _strip_symbols(URL_RE.sub("", line))
        clean = re.sub(r"\s{2,}", " ", clean).strip()
        if clean.endswith(":") and len(clean) < 60:  # header in front of a link list
            continue
        clean = clean.strip(STRIP_CHARS)
        if len(clean) < 3:
            continue
        paragraphs[-1].append(clean)

    text = ""
    for para in (p for p in paragraphs if p):
        chunk = " ".join(para)
        text = f"{text} {chunk}".strip()
        if len(text) >= limit:
            break
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return _truncate(text, limit) if text else "(No description.)"


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%a %d %b %Y, %H:%M %Z")


def build_digest(new: list[Video], failures: list[tuple[Channel, str]], tz: ZoneInfo, now: datetime) -> str:
    lines = [f"YouTube digest: {now.astimezone(tz).strftime('%A %d %B %Y')}"]
    if new:
        n_ch = len({v.channel_id for v in new})
        lines.append(f"{len(new)} new upload{'s' * (len(new) != 1)} from {n_ch} channel{'s' * (n_ch != 1)}")
    else:
        lines.append("No new uploads.")

    by_channel: dict[str, list[Video]] = {}
    for v in new:
        by_channel.setdefault(v.channel, []).append(v)

    for channel in sorted(by_channel, key=str.casefold):
        lines += ["", "=" * 60, channel.upper(), "=" * 60]
        for v in sorted(by_channel[channel], key=lambda x: x.published, reverse=True):
            lines += [
                "",
                f"Channel:  {v.channel}",
                f"Title:    {v.title}",
                f"Date:     {fmt_local(v.published_dt, tz)}",
                f"Summary:  {v.summary}",
                f"Link:     {v.url}",
            ]

    if failures:
        lines += ["", "-" * 60, "Could not fetch these channels this time:"]
        lines += [f"  - {c.name} ({c.id}): {err}" for c, err in failures]
    lines.append("")
    return "\n".join(lines)


def build_atom(items: list[Video], cfg: dict, tz: ZoneInfo, now: datetime) -> bytes:
    ET.register_namespace("", ATOM)

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, f"{{{ATOM}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    feed = ET.Element(f"{{{ATOM}}}feed")
    sub(feed, "title", cfg["feed_title"])
    sub(feed, "id", "urn:yt-digest:feed")
    sub(feed, "updated", now.isoformat())
    author = sub(feed, "author")
    sub(author, "name", "yt-digest")
    for v in items:
        e = sub(feed, "entry")
        sub(e, "title", f"{v.channel}: {v.title}")
        sub(e, "link", rel="alternate", href=v.url)
        sub(e, "id", f"yt:video:{v.id}")
        sub(e, "published", v.published)
        sub(e, "updated", v.published)
        a = sub(e, "author")
        sub(a, "name", v.channel)
        body = f"Channel: {v.channel}\nTitle: {v.title}\nDate: {fmt_local(v.published_dt, tz)}\n\n{v.summary}"
        sub(e, "content", body, type="text")
    ET.indent(feed)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def email_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("DIGEST_TO"))


def send_email(subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("DIGEST_FROM") or user
    msg["To"] = os.environ["DIGEST_TO"]
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def run(
    cfg: dict,
    base: Path,
    get: Callable[[str], bytes] = http_get,
    send: Callable[[str, str], None] | None = None,
    now: datetime | None = None,
    print_digest: bool = False,
) -> int:
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(cfg["timezone"])
    channels = load_channels(base / cfg["channels_file"])
    if not channels:
        print(f"No channels in {cfg['channels_file']}. Run import-takeout or add first.", file=sys.stderr)
        return 2

    state_path = base / cfg["state_file"]
    state = load_state(state_path)
    items: dict[str, dict] = state.get("items", {})

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        results = list(pool.map(lambda c: (c, *fetch_channel(c, cfg, get)), channels))

    failures = [(c, err) for c, _, err in results if err]
    fetched = [v for _, vids, _ in results for v in vids]

    lookback = now - timedelta(hours=cfg["lookback_hours"])
    if state.get("last_run"):
        lookback = min(lookback, datetime.fromisoformat(state["last_run"]) - timedelta(hours=2))
    retain = now - timedelta(days=cfg["retain_days"])
    lookback = max(lookback, retain)

    new: list[Video] = []
    for v in fetched:
        if v.id in items or v.published_dt < retain:
            continue
        items[v.id] = asdict(v)
        if v.published_dt >= lookback:
            new.append(v)

    items = {k: d for k, d in items.items() if datetime.fromisoformat(d["published"]) >= retain}

    digest = build_digest(new, failures, tz, now)
    all_items = sorted((Video(**d) for d in items.values()), key=lambda v: v.published, reverse=True)
    for rel, data in (
        (cfg["digest_file"], digest.encode("utf-8")),
        (cfg["feed_file"], build_atom(all_items[: cfg["feed_max_items"]], cfg, tz, now)),
    ):
        out = base / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)

    if print_digest:
        print(digest)

    ok = len(channels) - len(failures)
    print(f"{len(new)} new upload(s); {ok}/{len(channels)} channels fetched; {len(failures)} failed.")

    if ok == 0:
        print("Every channel failed; state left unchanged so nothing is lost.", file=sys.stderr)
        return 1

    if send and (new or cfg["send_empty"]):
        subject = f"YouTube digest: {len(new)} new" if new else "YouTube digest: nothing new"
        send(subject, digest)  # raises on failure -> state not saved -> retried next run
        print("Digest emailed.")

    state.update(version=1, last_run=now.isoformat(), items=items)
    save_state(state_path, state)
    return 0


def resolve_channel(ref: str, get: Callable[[str], bytes] = http_get) -> Channel:
    ref = ref.strip()
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", ref)
    if m and (CHANNEL_ID_RE.match(ref) or "/channel/" in ref):
        cid = m.group(1)
    else:
        if ref.startswith("http"):
            url = ref
        else:
            url = "https://www.youtube.com/" + (ref if ref.startswith("@") else "@" + ref)
        page = get(url).decode("utf-8", "replace")
        found = None
        for pattern in (
            r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})',
            r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
            r'<meta itemprop="identifier" content="(UC[A-Za-z0-9_-]{22})"',
            r'"browseId":"(UC[A-Za-z0-9_-]{22})"',
        ):
            hit = re.search(pattern, page)
            if hit:
                found = hit.group(1)
                break
        if not found:
            raise FetchError(f"could not find a channel ID on {url}")
        cid = found
    name = cid
    try:
        root = ET.fromstring(get(FEED_URL.format(param="channel_id", value=cid)))
        name = (
            root.findtext("atom:author/atom:name", default="", namespaces=NS)
            or root.findtext("atom:title", default="", namespaces=NS)
            or cid
        ).strip()
    except Exception:  # name is cosmetic; the ID is what matters
        pass
    return Channel(cid, name)


def import_takeout(csv_path: Path) -> list[Channel]:
    """Google Takeout > YouTube > subscriptions/subscriptions.csv
    Columns: Channel Id, Channel Url, Channel Title (header text may be localised)."""
    channels = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            cid = next((c.strip() for c in row if CHANNEL_ID_RE.match(c.strip())), None)
            if not cid:
                continue  # header or blank line
            name = row[2].strip() if len(row) > 2 and row[2].strip() else cid
            channels.append(Channel(cid, name))
    return channels


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default="config.toml", help="path to config.toml (default: %(default)s)")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("import-takeout", help="add channels from a Takeout subscriptions.csv")
    s.add_argument("csv", type=Path)
    s = sp.add_parser("add", help="add channels by URL, @handle or channel ID")
    s.add_argument("refs", nargs="+")
    s = sp.add_parser("run", help="build the digest and feed; email if SMTP is configured")
    s.add_argument("--print", action="store_true", help="also print the digest to stdout")
    s.add_argument("--no-email", action="store_true", help="skip email even if SMTP is configured")
    args = p.parse_args(argv)

    cfg_path = Path(args.config).resolve()
    base = cfg_path.parent
    cfg = load_config(cfg_path)
    channels_path = base / cfg["channels_file"]

    if args.cmd == "import-takeout":
        found = import_takeout(args.csv)
        added = append_channels(channels_path, found)
        print(f"{len(found)} subscriptions in export; {added} added to {cfg['channels_file']}.")
        return 0

    if args.cmd == "add":
        resolved = []
        for ref in args.refs:
            try:
                ch = resolve_channel(ref)
                resolved.append(ch)
                print(f"{ch.id}  {ch.name}")
            except Exception as e:
                print(f"could not resolve {ref!r}: {e}", file=sys.stderr)
        added = append_channels(channels_path, resolved) if resolved else 0
        print(f"{added} added to {cfg['channels_file']}.")
        return 0 if resolved else 1

    send = send_email if (email_configured() and not args.no_email) else None
    return run(cfg, base, send=send, print_digest=args.print)


if __name__ == "__main__":
    sys.exit(main())
