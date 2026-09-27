# yt-digest

A public, text-only web page and RSS feed of new uploads from your YouTube subscriptions, hosted free on GitHub Pages. Each upload shows:

```
Channel:  Example Channel
Title:    How the Danube Delta Formed   (links to the video)
Date:     Sat 26 Sep 2026, 18:30 BST
Summary:  The first couple of sentences of the description, with links,
          timestamps, hashtags, sponsor and social-media lines removed.
```

No thumbnails or images, no view counts, no notifications. It only ever lists uploads from the channels in `channels.txt`.

Every run writes:

- `output/index.html`: a plain web page, newest first, readable on a phone, light or dark to match your system; links open in a new tab
- `output/feed.xml`: an Atom feed (a kind of RSS feed) of the same items, for any reader app
- `output/digest.txt`: a local text list of what is new since the last run (not published)

Standard-library Python 3.11+, no dependencies (on Windows, add `tzdata`, see below).

## Before you start: Linux/macOS vs Windows

Run every command from the folder that contains `yt_digest.py`.

| | Linux / macOS | Windows (PowerShell) |
|---|---|---|
| Python command | `python3` | `python` (`python3` usually opens the Microsoft Store) |
| Time zones | built in | run once: `python -m pip install tzdata` |

Without `tzdata`, Windows can't look up time zones, and the script stops with: `Time zone "Europe/London" not found. Run: python -m pip install tzdata`.

The examples below use `python`. On Linux/macOS, type `python3` instead.

## 1. Build your channel list

1. Go to [Google Takeout](https://takeout.google.com), deselect everything, select **YouTube and YouTube Music**, and under "All YouTube data included" keep only **subscriptions**.
2. Download the export and find `subscriptions.csv` in it.
3. Import it, using the real path to the file (in quotes if it contains spaces):

   ```powershell
   python yt_digest.py import-takeout "C:\Users\you\Downloads\Takeout\YouTube and YouTube Music\subscriptions\subscriptions.csv"
   ```

To add channels later: `python yt_digest.py add @handle https://www.youtube.com/@another UCxxxxxxxxxxxxxxxxxxxxxx`

To stop following a channel, delete or comment out its line in `channels.txt`.

## 2. Try it locally

```powershell
python yt_digest.py run --print
```

Then open `output/index.html` in your browser. `--print` also shows the text list of new uploads.

## 3. Publish it on GitHub Pages

The repo has to be **public** for free GitHub Pages. That means anyone can see `channels.txt` (your subscription list) and the `state` branch (recently seen videos).

1. **Create the repo.** On <https://github.com/new>, name it `my-yt-subs-feed`, choose **Public**, and leave "Add a README" unticked.
2. **Push this folder** from the folder that contains `yt_digest.py`. Replace `YOUR-USER` with your GitHub user name:

   ```powershell
   git add -A
   git commit -m "Public text-only feed"
   git remote add origin https://github.com/YOUR-USER/my-yt-subs-feed.git
   git push -u origin main
   ```

3. **Turn on Pages.** In the repo, open **Settings → Pages**, and under **Build and deployment → Source** choose **GitHub Actions**.
4. **Run it once.** Open **Actions → YouTube feed → Run workflow**. When both jobs (`build` and `deploy`) show a green tick, the site is live.
5. **Check `site_url`** in `config.toml`. It must match your address, `https://YOUR-USER.github.io/my-yt-subs-feed/` (GitHub uses lower case). If you use a different user or repo name, change it there.

After that, the workflow runs by itself every 6 hours (at minute 17, UTC; GitHub often starts it a little late), plus once just after Sunday midnight UK time for the weekly reset. Public repos get GitHub Actions free.

- Web page: `https://YOUR-USER.github.io/my-yt-subs-feed/`
- Feed: `https://YOUR-USER.github.io/my-yt-subs-feed/feed.xml`

### Weekly reset and the memory file

- **Weekly reset:** from Monday 00:00 (in the `timezone` set in `config.toml`), the page and feed start empty and fill up again as new videos arrive. Nothing older than 7 days is ever kept. Videos your reader app already downloaded stay in the app.
- **Memory file:** `state.json` records which videos the job has seen. It is kept on a separate branch called `state`, which only ever holds its latest copy. The main branch doesn't change on each run, so the repo doesn't grow over time.

## 4. Add the feed to a reader app

In any RSS reader (for example Feedly, Inoreader, NetNewsWire, Feeder on Android, or Thunderbird), choose "Add feed" or "Subscribe", and paste either the feed address or just the page address. The page advertises its feed, so readers can find it.

Switch the reader to a list or text view. Some "card" layouts fetch preview images from the video page even though the feed has none.

## Settings

All in `config.toml`: `site_url`, time zone (`Europe/London` by default; `Europe/Sofia` for Bulgarian time), summary length, whether to exclude Shorts and past livestreams, how many days to keep (`retain_days`, 7), the weekly reset (`weekly_reset`, on), and file paths.

## How the summary works

It is extractive, not AI-generated: it keeps the opening prose of the description and drops link lists, chapter timestamps, hashtags, sponsor/discount-code lines, social-media and merch plugs, and emoji. It is then trimmed to about 280 characters at a sentence boundary. Descriptions that are nothing but links show "(No description.)".

All text from YouTube is escaped on the page, meaning characters like `<` and `&` are shown as text and can never act as HTML.

## Notes and limits

- YouTube's feeds only list each channel's 15 most recent uploads. A channel that posts more than 15 videos between two runs (6 hours) may have some missed.
- Videos-only mode uses the channel's long-form uploads playlist (`UULF…`). If a channel has none, it falls back to the full channel feed and filters Shorts by URL.
- YouTube's feed endpoint occasionally returns errors for a channel. It is picked up on the next run.
- To pause updates, open **Actions → YouTube feed → ⋯ → Disable workflow**. GitHub may also pause scheduled runs in a public repo after 60 days with no activity; the Actions tab then shows a button to turn them back on.

## Tests

```powershell
python -m unittest -v
```
