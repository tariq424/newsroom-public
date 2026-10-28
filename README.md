# The Newsroom

Google Alerts → **Koda** (Muse) → **Newsroom Inbox** Google Doc → Video Studio → Staging → YouTube Publisher.
A local Flask app on **http://127.0.0.1:42040** (`F:\code\newsroom`, private repo `tariq424/newsroom`). It talks to
Video Studio (42021) and the YouTube Publisher (42030) over HTTP only; it imports neither. Everything runs on the PC by
itself: no Claude session is needed for the queue, the locks or publishing.

## The Inbox Doc is the scoreboard
| Line | Written by | When |
|---|---|---|
| `[PENDING] Headline \| link \| pitch [\| RESEARCH BRIEF: …]` | Koda | it picks a story (optional `[BREAKING]` before the headline) |
| `[PROCESSED] Headline - JobID` | Newsroom | the video is finished and in Staging |
| `[PUBLISHED] Headline - YouTube link` | Newsroom | the upload finished |
| `[REJECTED] Headline - JobID` | Newsroom | you pressed Reject |

Items are matched by headline (case, punctuation and quote style ignored). Koda deletes every line of a story once it has
a `[PUBLISHED]` or `[REJECTED]` line, so the doc stays short; the Newsroom's own state keeps the history. Koda keeps a
rolling queue of up to 20 unprocessed stories (2 new per hour, 10 per day). Rules for Koda: **MUSE_INSTRUCTIONS.md**.

## Production (automatic)
- The doc is read **once an hour**. One video is made at a time; the next new one starts when the pipeline is idle:
  1. a `[BREAKING]` item (it also pauses an ordinary render, which resumes automatically after),
  2. a paused video (resumes; exempt from the limits),
  3. the oldest new item (FIFO).
- Limits for new videos: one per hour (`min_minutes_between_videos` 60; breaking skips it) and 10 in any rolling 24 hours
  (`max_videos_per_day`). Videos waiting for review never stop production. `F:\videos\newsroom\hold` pauses all new work.
- `mode` both: a Short plus a 15-minute explainer per story. A research brief is added to the article text as labelled
  background (with 150+ words it stands in for an unreadable article). A failed item is retried once.
- **Never killed:** Pause stops a job at its next safe point; a crashed or interrupted job becomes **stalled** with
  Resume (continue) and Retry (start over; the old files stay).
- **Interlocks:** one Newsroom process (OS lock on `F:\videos\newsroom\newsroom.lock`), one job started at a time (own
  records plus Video Studio idle), one upload at a time.

## Review and publish
- **Staged video** panel: tabs **To review** and **Published**, each with its own Prev / Next (they wrap around). Player
  with scrubber; Short / Explainer switch; channel, visibility, Short / Explainer / Both.
- Tick **Approve for publish** on the videos you want (or **Approve all**), then **Publish approved**: the Newsroom
  uploads them one after another in tick order (a failure doesn't stop the rest). Single Publish and Reject still work.
- Both = explainer first, then the Short with the explainer's link. The other part can be published later.
- Videos with AI-generated visuals get `ai_content.json`; the Publisher answers YouTube's "Altered content" with Yes.
- **Phone page** `http://<PC address>:42040/m?k=<passcode>` (passcode in `F:\videos\newsroom\mobile_pin.txt`, never in
  git): Staging only, publish or reject. Away from home via Tailscale.
- Header: `Queue (oldest first): N waiting · N making · N to review · N published today · N/10 made in 24 h · waiting: …`.

## Settings (config.json)
`mode`, `minutes` (15), `short_secs`, `audience`, `channels`, `visibility`, `doc_id`, `inbox_source`, `chrome_profile`,
`headless`, `offscreen_window`, `inbox_refresh_minutes` (60), `ignore_keywords`, `auto_generate`, `auto_retries`,
`min_minutes_between_videos`, `max_videos_per_day`.

## Running
`scripts\start-newsroom.ps1` (the Video Studio fork's Python). `F:\code\restart-stack.ps1` starts everything; it refuses a
full restart while Video Studio renders (use `force` in `F:\code\.restart-request-text`). `F:\code\.restart-only` with
`newsroom` / `publisher` restarts only those; `login-<site>` opens a sign-in window; `imagegen-poc[-site]` runs the
image-service smoke test. Logs: `F:\videos\newsroom\newsroom.stdout.log` / `.stderr.log`. State:
`F:\videos\newsroom\newsroom_state.json` (a `state_import.json` next to it is merged in at start-up).

## Tests
`python -m pytest -q tests` (fake Video Studio, Publisher and news site on local ports plus a local inbox file).
