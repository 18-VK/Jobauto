# How scheduling works

Written for whoever needs to change this, or work out why a run did or did not
happen. For *using* it, see the schedule section of the
[README](../README.md#running-it-daily-on-its-own).

---

## The constraint that shapes everything

**There is no cron.** Free hosting gives you a web process that sleeps after
about fifteen minutes of no traffic, and a sleeping process runs no background
threads, no timers, and no scheduler. Anything that had to fire at 09:00 on the
server simply would not.

What *does* happen reliably is the agent polling `GET /api/agent/work` every
thirty seconds, from a machine that is awake because someone is using it.

So **the agent's poll is the clock.** The server never decides "it is 09:00, go";
it answers "you are asking, and it is now past 09:00 and today's run has not
happened, so here is some work."

The consequence is deliberate and worth stating plainly: **if the PC is off at
the scheduled time, the run starts when it next comes online** rather than being
skipped. Late is more useful than never for this. It also means the schedule is
approximate — a run fires within one poll interval of its time, not on the
second.

---

## A run is a chain, not a job

```
   09:00  ─▶  discover  ─▶  apply 5  ─▶  apply 5  ─▶  apply 5  ─▶  apply 5
                 │             │            │            │            │
              queued        queued       queued       queued       queued
              by the      only after   only after   only after   only after
            schedule      the last     the last     the last     the last
                          reported     reported     reported     reported
```

Each link is queued only once the previous one **reports back**. Nothing is
queued ahead of time.

That is not a stylistic choice. If the whole chain were queued up front and the
agent then died, five tasks would sit in the queue and all run at once when it
returned — five browser sessions, five times the portal traffic, at a moment
nobody chose. Queueing one at a time means a stalled agent stalls the chain,
which is the correct behaviour.

### Why batches at all

Applications are paced on purpose — see [RISKS.md](RISKS.md). A batch is also
the natural place to stop: if a batch produces nothing, there is nothing left
worth doing today.

---

## When a run is due

`schedule.is_due(settings, last_run, now)` answers yes when **all** of:

1. the schedule is enabled
2. today, in the user's timezone, is one of the configured days
3. `now` is at or past today's scheduled time
4. `last_run` is earlier than today's scheduled time

Point 4 is what makes it once a day rather than once every poll: the first poll
past 09:00 sets `User.schedule_last_run`, and every poll after that sees a
`last_run` newer than today's slot.

Point 3 is a comparison, not an equality — which is the whole reason a late
start works.

### Timezones

The server runs in UTC. `09:00` has to mean 09:00 where the user is, so the
schedule stores a timezone and the comparison is done in it:

```python
local = datetime.combine(date, time(9, 0), tzinfo=ZoneInfo("Asia/Kolkata"))
due   = local.astimezone(timezone.utc)      # 03:30 UTC
```

An unknown timezone name falls back to UTC rather than raising — a typo should
make the schedule slightly wrong, not wedge it. Same for a malformed time.

---

## What stops the chain

`schedule.next_step` is called when a task reports a result, and returns nothing
— ending the chain — in any of these cases:

| Condition | Why |
|---|---|
| The finished task was not `scheduled` | A *Search portals* you pressed by hand must not silently become twenty applications |
| The schedule is disabled, or `apply: false` | You asked for searching only |
| `discover` failed | Applying against a failed search would work from stale data |
| `batch > max_batches` | The daily ceiling you set |
| The last batch produced nothing | Empty shortlist, or a daily cap hit — further batches are noise |

"Produced nothing" means `prepared + submitted + external == 0` in the task
result.

Note what is **not** in that list: a discover that found zero *new* jobs still
chains into apply. Finding nothing new is not the same as having nothing to
apply to — jobs found on previous days may still be unapplied.

---

## Where it runs in the request cycle

Two hooks, both inside handlers that were going to run anyway:

```python
# GET /api/agent/work  -- the agent asking for something to do
schedule.maybe_start(s, uid)        # start today's run if due

# POST /api/agent/tasks/<id>/result -- the agent reporting back
schedule.next_step(s, user, task)   # queue the next link, if any
```

`maybe_start` also refuses to start when anything is already queued or running
for that user, so a scheduled run never stacks on top of work in progress.

A failure in `next_step` is caught and logged rather than propagated: the task
result has already been recorded by that point, and losing it because the
*next* step could not be queued would be worse than a broken chain.

---

## Timing constants

All of these interact, so they are listed together. Changing one usually means
checking another.

| Constant | Value | Why that value |
|---|---|---|
| Agent poll interval | 30 s | The schedule's resolution, and the delay before queued work starts |
| Max backoff | 120 s | Must stay under the 3 min online window, or a backing-off agent reads as offline |
| Progress push | 5 s | A discover emits a line per job; pushing each one would hammer a free tier |
| Claim grace | 60 s | A task claimed this recently may belong to a poll still in flight |
| Agent silent | 5 min | The agent heartbeats every 20 s while working, so this much silence means gone |
| Task ceiling | 6 h | Backstop for an agent alive but wedged on a task it will never finish |
| Queued expiry | 24 h | Long enough to queue from a phone and have it run that evening |
| Purge interval | 12 h | Retention housekeeping, also driven off the poll |

---

## How a run recovers when it goes wrong

Scheduling creates tasks, so everything that can strand a task applies here.
Three separate signals, because there are three distinct failures:

**The agent restarted or crashed.** It runs a task synchronously and only polls
again once idle, so an agent *asking for work* while one of its tasks is still
`running` has demonstrably abandoned it. Cleared on the next poll, after the
60 s grace.

**The agent is gone entirely.** It heartbeats every 20 s while working, so five
minutes of silence means stopped rather than busy.

**The agent is alive but wedged.** Six hour ceiling.

A run stranded by any of these ends with a failed task, and the chain stops —
`next_step` only continues from a task whose status is `done`. Tomorrow's run is
unaffected, because `is_due` looks at `schedule_last_run`, not at whether the
run succeeded.

Reaping also happens when the browser reads `/api/summary` or `/api/tasks`, so
opening the dashboard clears a stranded task with no agent involved.

---

## The read side

Two more functions exist purely so the dashboard can show the schedule without
duplicating any of the logic above.

`settings_for(user)` parses the `schedule:` block out of the user's preferences
YAML and layers it over `DEFAULTS`, so an absent key means the default rather
than a crash, and a preferences file the user has broken falls back wholesale
instead of disabling the schedule silently.

`next_run(settings, after=None)` walks forward up to eight days and returns the
first configured day whose scheduled time is still in the future — eight because
a week plus one covers the case where today's slot has already passed. It powers
the *next run* line under the schedule panel and returns `None` when the
schedule is disabled or has no days selected.

Both are read-only. Nothing about whether a run *happens* depends on them, so a
bug there is a display bug, not a missed run.

`GET /api/schedule` returns the settings, the computed next run, the last run,
and `batch_size × max_batches` as `per_day` — that last one only so the UI can
say "up to 20 applications a day" without recomputing it.

---

## Storage

| What | Where | Why there |
|---|---|---|
| Schedule settings | `schedule:` block in the user's preferences YAML | Editable from the dashboard with the PC off, and the agent already syncs preferences |
| Last run | `users.schedule_last_run` | Needs to be authoritative across workers; a per-process variable would fire once per worker |
| Chain position | `tasks.payload_json` → `{"scheduled": true, "batch": 2}` | Travels with the task, so no separate state to keep in step |

The `schedule_last_run` column is added to existing deployments by
`ensure_columns`, since `create_all` only creates missing tables.

---

## Things that look like bugs and are not

**"It ran at 09:04, not 09:00."** Within one poll interval. Expected.

**"It ran at 14:00."** The PC was off at 09:00 and came online at 14:00.

**"It did not run at all."** Either the PC has not been online since the
scheduled time, or `schedule_last_run` is already past today's slot. Both are
visible: the dot in the dashboard header, and **Devices → Recent activity**.

**"Only one apply batch ran."** That batch produced nothing, so the chain
stopped. Look at the task's result in Recent activity.

**"Applications were prepared but not sent."** That is the whole design. See
[RISKS.md](RISKS.md).

---

## Testing it

`tests/test_schedule.py` covers the timing logic without waiting for real time
to pass — `is_due` takes an explicit `now`, so a late start, a day boundary, a
timezone offset and an excluded weekday are all ordinary assertions.

The chain tests drive the real endpoints: queue, claim via `/api/agent/work`,
report via `/api/agent/tasks/<id>/result`, and assert what the next poll hands
back. That covers the wiring, not just the pure functions.

---

## Keeping the agent alive

Everything above depends on one thing: **the agent is running**. It is the
clock. If it stops, the dashboard says offline and the daily run silently never
fires — which looks identical to the whole thing being broken.

So autostart is not a preference. `jobauto autostart` registers one Windows
scheduled task with two triggers:

| Trigger | Covers |
|---|---|
| At logon, delayed 1 minute | a fresh session, with the network up first |
| Every 15 minutes, forever | an agent that stopped for any other reason |

The second one is the important half, and it only works because of
`MultipleInstancesPolicy = IgnoreNew`. The heartbeat fires whether or not the
agent is up; when it is, Windows discards the new instance and nothing happens.
When it is not — crashed, killed by a policy sweep, never started, machine
logged in for three weeks — that same trigger is what brings it back.

Neither setting does this alone. `RestartOnFailure` only covers a process that
exits non-zero, and a logon trigger only covers logging on. The pair of them is
what makes it self-healing.

```bash
jobauto autostart            # set it up and start it now
jobauto autostart --status   # registered? running? is the schedule even on?
jobauto autostart --remove   # stop it starting itself
```

`--status` answers the whole question rather than the half it is named after: a
running agent with the schedule switched off looks exactly like a stopped agent
from the outside — both are "nothing happens all day".

### Why registering can be refused

`register()` will not create a task whose command cannot start. The usual case
is a source checkout, where `python -m jobauto` works only with `PYTHONPATH`
set — which a scheduled task does not inherit. A task that fails every fifteen
minutes is worse than no task at all: the dashboard says offline, and the only
evidence is a "Last Result" number in a UI nobody opens.

That number is translated for you:

| Last Result | Means |
|---|---|
| `0` | exited cleanly — it stopped rather than crashed |
| `1` | jobauto is not installed in the Python the task runs |
| `2` | this PC is not linked to your dashboard |
| `267009` | running now |
| `267011` | has not run yet |

### What still is not automatic

**Submitting.** The schedule runs discover, then fills applications in batches,
and stops. Every one waits for you to review and send — see the review gate in
[RISKS.md](RISKS.md). That is the design, not a missing feature.

**Signing in to the portals.** Once per portal, by hand, in a real browser. No
password is ever stored, so there is nothing to automate.
