## Summary

<!-- What does this pull request change, and why? -->

## Type

- [ ] Code (no file under `registry/` changes, except `registry/schema.json` with a schema change)
- [ ] Registry data (only files under `registry/` change)

A pull request may not mix the two: the `Registry gate` check fails when it does.

## Registry data: evidence checklist

Fill this in for every record whose status changes or is not `scanned`. CI checks
only that each record's claimed last activity exists and that no own activity is
hidden after it. **CI does not check `baseline_posts`, `baseline_active_days` or
`cliff`: reviewers check them against the evidence file.**

- [ ] Each `verified_inauthentic` record has its evidence file at
      `registry/events/<event-id>/evidence/<platform>/<id_b64>.md`. Every record moving
      into `verified_inauthentic` or `cleared`, every change to such a record, every
      evidence change and every `reddit` or `x` record needs the repository owner's
      approval (the `registry-override` label and the owner's approving review of the
      head commit).
- [ ] A change to `events.toml` only adds events, or a maintainer reviewed the change
      to an existing event's name, cutoff or windows (it needs the `registry-override`
      label and an approving review of the head commit).
- [ ] I compared `baseline_posts` and `baseline_active_days` with the account's public
      history in the baseline window (the evidence file's hour profile and activity
      links).
- [ ] The account really went silent: `cliff` is true, the last activity in the
      evidence matches the record, and nothing newer appears on the live profile up to
      `scan_window_end`.
- [ ] I identified the account by its ID (the Bluesky DID; the Reddit author name),
      not by a handle that may have changed hands.
- [ ] Bluesky: I read the profile itself, not only the feed query. A post whose
      `createdAt` is backdated before the window is listed at its `createdAt` position,
      so the scan and the spot-check can both miss it.
- [ ] The behavior is inauthentic, not a person who stopped posting or an ordinary
      user of the services the outage cut off. A stop at the outage is not evidence on
      its own: the evidence file's reviewer findings record the live profile check
      (active, suspended or deleted) and at least one finding beyond the silence, and
      the gate refuses a verdict without them. When in doubt, the status is `cleared`.
- [ ] Each new `verified_inauthentic` record has at least its event's
      `verified_min_silence_days` (21 by default) of silence, and its account has not
      posted again since.
- [ ] Records are written by `registry add` or `registry merge` and pass
      `registry validate`.

- [ ] Every `verified_inauthentic` to `cleared` change has its reason stated in the
      summary above. **Reviewers must confirm that reason before approving:** the gate
      needs no label to clear a record, and lists each one as a high-impact transition.

Removing records, moving a record from `verified_inauthentic` or `cleared` back to an
automatic status, changing more than 15 records, status changes on `reddit` or `x`
(which CI cannot query), username changes of `verified_inauthentic` or `cleared`
records, and evidence file changes without a transition of the same record into
`verified_inauthentic` need a maintainer override. The maintainer approves the head
commit in a review and then adds the `registry-override` label; the gate honours the
label only with an approving review of that same head commit by someone with write
access other than the author. The label is removed after every new push; review and
approve the new head, then add the label again. If the label was added before the
approval, rerun the `Registry gate` after approving.
