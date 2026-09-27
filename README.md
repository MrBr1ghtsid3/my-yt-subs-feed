# yt-digest

A plain-text digest of your YouTube subscriptions. Each upload appears as:

```
Channel:  Example Channel
Title:    How the Danube Delta Formed
Date:     Sat 26 Sep 2026, 18:30 BST
Summary:  The first couple of sentences of the description, with links,
          timestamps, hashtags, sponsor and social-media lines removed.
Link:     https://www.youtube.com/watch?v=...
```

No thumbnails, no view counts, no push notifications. It produces:

- `output/digest.txt`: new uploads since the last run, grouped by channel (also sent by email when configured)
- `output/feed.xml`: a rolling, text-only Atom feed of the last 30 days, for any RSS reader

Standard-library Python 3.11+, no dependencies.

## 1. Build your channel list

1. Go to [Google Takeout](https://takeout.google.com), deselect everything, select **YouTube and YouTube Music**, and under "All YouTube data included" keep only **subscriptions**.
2. Download the export and find `subscriptions.csv` in it.
3. Import it:

   ```bash
   python3 yt_digest.py import-takeout path/to/subscriptions.csv
   ```

To add channels later: `python3 yt_digest.py add @handle https://www.youtube.com/@another UCxxxxxxxxxxxxxxxxxxxxxx`

To stop following a channel, delete or comment out its line in `channels.txt`.

## 2. Try it locally

```bash
python3 yt_digest.py run --print --no-email
```

The first run covers the last 48 hours. Every run after that covers everything since the previous run, so a missed day is caught up rather than lost.

## 3. Daily email via GitHub Actions (recommended)

1. Create a **private** repository (your subscription list is in `channels.txt`) and push this folder to it.
2. For Gmail: turn on 2-Step Verification, then create an **app password** at <https://myaccount.google.com/apppasswords>.
3. In the repo, open **Settings → Secrets and variables → Actions** and add:

   | Secret | Value (Gmail) |
   |---|---|
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `465` |
   | `SMTP_USER` | your Gmail address |
   | `SMTP_PASSWORD` | the 16-character app password |
   | `DIGEST_TO` | where the digest should go |
   | `DIGEST_FROM` | optional; defaults to `SMTP_USER` |

4. Open **Actions → YouTube digest → Run workflow** once to test it.

After that it runs daily around 05:43 UTC (edit the `cron` line to change it). Each run commits `state.json` back to the repo so videos are never sent twice. A failed email leaves the state unchanged, so those videos go out in the next run, and GitHub emails you about the failed run.

A daily run takes well under a minute, which fits comfortably inside the free Actions allowance for private repos.

## 4. Or run it on your own machine (systemd)

`~/.config/systemd/user/yt-digest.service`

```ini
[Unit]
Description=YouTube text digest

[Service]
Type=oneshot
WorkingDirectory=%h/yt-digest
EnvironmentFile=%h/yt-digest/.env
ExecStart=/usr/bin/python3 yt_digest.py run
```

`~/.config/systemd/user/yt-digest.timer`

```ini
[Unit]
Description=Daily YouTube text digest

[Timer]
OnCalendar=*-*-* 07:30
Persistent=true

[Install]
WantedBy=timers.target
```

Put the same `SMTP_*` / `DIGEST_*` variables in `.env` as `KEY=value` lines (it is git-ignored), then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now yt-digest.timer
```

To use the feed instead of email, point a reader at `output/feed.xml` (e.g. as a local file in NewsFlash or Liferea) and switch the reader to a list/text view. Some "card" layouts fetch preview images from the video page even when the feed carries none.

## Settings

All in `config.toml`: timezone (`Europe/London` by default; `Europe/Sofia` for Bulgarian time), summary length, whether to exclude Shorts and past livestreams, whether to email on empty days, and file paths.

## How the summary works

It is extractive, not AI-generated: it keeps the opening prose of the description and drops link lists, chapter timestamps, hashtags, sponsor/discount-code lines, social-media and merch plugs, and emoji. It is then trimmed to about 280 characters at a sentence boundary. Descriptions that are nothing but links show "(No description.)".

## Notes and limits

- YouTube's feeds only list each channel's 15 most recent uploads. Channels that post more than 15 videos a day may have some missed.
- Videos-only mode uses the channel's long-form uploads playlist (`UULF…`). If a channel has none, it falls back to the full channel feed and filters Shorts by URL.
- YouTube's feed endpoint occasionally returns errors for a channel. Those channels are listed at the bottom of the digest and picked up on the next run.
- To pause the digest, disable the workflow in the Actions tab.

## Tests

```bash
python3 -m unittest -v
```
