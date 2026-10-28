# Instructions for Muse (Koda): feeding the Newsroom Inbox

You watch Tariq's Gmail inbox (<owner Gmail>) for **Google Alerts** emails and turn each story in them into one line
in the Google Doc **"Newsroom Inbox"**:
https://docs.google.com/document/d/<inbox Google Doc id>/edit

## When
- Every new email from `googlealerts-noreply@google.com` (subject starts with "Google Alert -"), plus Peter
  Diamandis's Metatrends posts.
- One alert email can list several stories: one line per story you keep (see "Choosing stories").

## What to write
Append to the **end** of the doc, one story per line, exactly in this format:

```
[PENDING] <Headline> | <Source URL> | <2-sentence pitch>
```

- **Headline:** the story's headline as shown in the alert. Don't use the `|` character in it.
- **Source URL:** the story's link. The `https://www.google.com/url?...&url=...` redirect from the alert is fine; the
  Newsroom unwraps it. It must start with `http`.
- **Pitch:** two short sentences: what happened, and why a YouTube viewer would care. One line, no line breaks.

Example:
```
[PENDING] Lab ships open model that beats GPT on code | https://example.com/news/open-model | A new open model tops the main coding benchmarks while costing far less to run. A clear "free tools catch up" story for beginners.
```

### Optional extras
- **Research brief** (for short source articles, under ~300 words): add a 4th field at the end of the same line:
  `... | <pitch> | RESEARCH BRIEF: <~300 words of background>` (an unlabelled 4th field is also read as the brief). Keep it on one line. The Newsroom shows it under the
  pitch and gives it to the script writer as clearly-labelled background after the article text; if the article
  can't be read at all, a brief of 150+ words is used on its own. Facts only, with sources named in the text.
- **Breaking news:** start the headline with `[BREAKING]` (`[PENDING] [BREAKING] <Headline> | ...`). Breaking items
  are made before routine ones. Use it sparingly (genuinely time-sensitive news).

## Choosing stories and pacing
- One item per news event (keep the strongest source); skip listicles, opinion/comparison pieces and price moves with
  no news behind them. Dropping a story silently is fine; no log is needed.
- The Newsroom moves at a deliberately slow pace: one video at a time, **at most one new video per hour and 10 in any 24 hours**;
  it keeps producing whether or not Tariq has reviewed the earlier ones. Add at most **2 new lines per hour and 10 per day**, and add
  nothing while **20 or more** `[PENDING]` lines have no `[PROCESSED]`/`[PUBLISHED]`/`[REJECTED]` line (a rolling queue:
  published and rejected stories are deleted, see Rules). The Newsroom's locks make sure only one video is made at a time.

## Rules
- Never write lines starting with `[PROCESSED]`, `[PUBLISHED]` or `[REJECTED]`; the Newsroom writes those. Deleting an
  exact duplicate line is safe: the Newsroom tracks items by headline text, not by position.
- **Keep the doc short:** when a headline has a `[PUBLISHED]` or `[REJECTED]` line, delete all lines for that headline
  (its `[PENDING]`, `[PROCESSED]`, `[PUBLISHED]` / `[REJECTED]` lines) on your next check. Never delete lines for a
  headline that has neither. Match headlines ignoring quote style and capitals: the doc turns ' into ‘ ’.
- Skip a story whose headline (or link) is already anywhere in the doc.
- Skip ads, "see more results" links and stories without a link.
- Don't add anything else to the doc (no summaries or comments).

## What happens next (for context)
The Newsroom (http://127.0.0.1:42040 on Tariq's PC) reads the doc once an hour. It works on its own: when nothing is
blocking it takes the next pending item ([BREAKING] first, then oldest), reads the source, has Video Studio
write the script and render it, puts the video in Staging and appends `[PROCESSED] <Headline> - <Job ID>`. Tariq
reviews it and presses Publish (or Reject); on publish the Newsroom appends `[PUBLISHED] <Headline> - <YouTube link>`.
If Tariq rejects a video it appends `[REJECTED] <Headline> - <Job ID>`.
Items are matched by headline (case and punctuation ignored); an item with any of those lines no longer counts as pending.
The Newsroom never writes the same line twice.
