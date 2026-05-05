# HN launch day — runbook

Pre-flight + day-of checklist for the v2.0.0 GA → HN submission. Use this in order; tick the boxes as you go. Should take ~3 hours of focused work the day-of.

---

## Pre-flight (T-7 days through T-24h)

- [ ] **Founder dogfood week complete** — see `DOGFOOD.md`
  - 1 week of real coding with codevira active
  - At least 2 different AI tools used on the same project (the wedge)
  - Wrap-up note written: did codevira save tokens? would you keep it?

- [ ] **3 alpha testers ran ≥ 2 days each** — see `docs/alpha-tester-invites.md`
  - 0 critical bugs reported
  - ≤ 2 friction issues (any more → fix → re-test)

- [ ] **v2.0.0 tag pushed**
  ```bash
  git tag -a v2.0.0 -m "v2.0 — 10 heroes + universality wedge"
  git push origin v2.0.0
  ```

- [ ] **PyPI release (the install one-liner has to actually work)**
  ```bash
  python -m build
  python -m twine upload dist/codevira-2.0.0*
  # Verify:
  pipx install codevira==2.0.0
  codevira doctor
  ```

- [ ] **Test the install on a brand-new machine / fresh user account**
  ```bash
  pipx install codevira
  cd ~/some-real-project
  codevira setup
  codevira doctor
  # All ✓ or only minor ⚠
  ```

- [ ] **README final pass** — open in browser, read top-to-bottom as if you've never seen the project
  - Wedge headline lands?
  - Quick Start works as written?
  - Demo video link works?
  - Differentiation page link works?

- [ ] **Demo video uploaded** to a hosting service (GitHub release attachment, Cloudflare Stream, asciinema, etc.) — get a public URL

- [ ] **Backup channels prepped**
  - GitHub Discussions enabled
  - GitHub Issues template up to date
  - You have direct DM contact for the 3 alpha testers (they'll catch overflow on launch day)

- [ ] **HN account warmed**
  - Account ≥ 30 days old, ≥ 5 karma — fresh accounts get filtered
  - Recent comment activity (you're a real person, not a bot)

---

## Day-of: T-2h before submit

- [ ] Re-run the Pre-flight one-time install on a clean shell
  ```bash
  pipx uninstall codevira  # nuke
  pipx install codevira
  cd ~/test-project
  codevira setup
  codevira doctor
  ```

- [ ] Verify the demo video plays for someone NOT on your machine — share the link to a friend, ask them to confirm playback works

- [ ] Open these tabs (you'll need them all):
  - GitHub repo
  - PyPI page
  - Demo video URL
  - `docs/vs-other-memory-tools.md`
  - HN submit page (don't load until ready to submit)
  - Twitter / X (for cross-post once HN goes up)

- [ ] Charge laptop. Quiet office. No meetings for 4h after submit.

---

## Submit window

**Best slot:** Tuesday or Wednesday, 9:00–10:00 AM Pacific (12:00–13:00 ET, 17:00–18:00 UTC).

Why: HN moderators are most active, most US/EU developers are starting their day, weekend submits get buried.

Avoid: Friday afternoon, weekends, US holidays.

### Title (under 80 chars)

```
Codevira – persistent memory + decision log for AI coding agents
```

Don't:
- Lead with "Show HN" — over-used; honest framing wins
- Use buzzwords (revolutionary, AI-powered, paradigm-shifting)
- Use ALL CAPS or emoji
- Mention the version number ("v2.0") — readers don't care

### URL field

```
https://github.com/<your-handle>/codevira
```

NOT the docs page, NOT the demo video. The HN audience grades repos.

---

## First comment (post within 60 seconds of submit)

You write this as the OP. Keep it under 200 words. Template:

> Hi HN — built this as a solo dev after getting frustrated re-explaining my codebase to AI agents every session, and losing carefully-made architectural decisions because the AI didn't remember them.
>
> Codevira is a local MCP server that gives every AI coding tool you use shared memory of your project. Open Claude Code in the morning, switch to Cursor at noon, finish in Windsurf — they all see the same decisions, file graph, and context.
>
> v2.0 ships 10 "AI guardian" capabilities that intercept every Edit / prompt the AI tries: decision protection (`do_not_revert`), anti-regression (block re-introducing fixed bugs), scope locks, intent inference, style enforcement, etc.
>
> Local-first, MIT, no signup, no SaaS. `pipx install codevira && codevira setup` and it auto-configures every AI tool you have.
>
> Honest comparison vs Mem0 / claude-mem / MemPalace / Zep: [link to docs/vs-other-memory-tools.md]
>
> 30-second demo: [link]
>
> Stuff I'm honestly still figuring out: [1-2 things you'd genuinely want feedback on, e.g., "the off-by-default scope lock — too restrictive? right balance?"]
>
> Happy to answer anything — I'll be here all day.

**Don't say**:
- "Built in N days" — sounds like fluff
- "Excited to share!" — anti-signal
- "Looking for feedback" — implicit; saying it is weak
- Anything that sounds defensive or apologetic

---

## First 4 hours after submit (the critical window)

- [ ] **Triage every comment within 15 min**
  - Substantive question → substantive reply
  - "Have you considered X?" → if yes, brief acknowledgment + your reasoning. If no, "good point, hadn't considered" + add to backlog
  - Critique → engage honestly. Don't be defensive. "You're right, that's a gap" beats "actually..." every time.
  - Praise → brief thanks, ask a follow-up if it opens conversation

- [ ] **Watch GitHub issues hourly**
  - Hot-fix anything obvious as v2.0.1
  - Create issues for non-trivial bugs; assign to yourself; don't promise dates

- [ ] **Stargazer DMs**
  - Reply to every DM. Personal touch matters at this scale.

- [ ] **Cross-post**
  - Twitter / X: same first-comment paragraph + HN link
  - Relevant subreddits: ONLY if you have karma there. r/programming and r/MachineLearning are fine; r/ChatGPTCoding allowed but lower signal.
  - Discord communities you're in: yes if you've been active

---

## What NOT to do

- ❌ **Don't sock-puppet votes**. HN catches it; account gets shadow-banned; you're done.
- ❌ **Don't reply to every single comment with the install command**. People will scroll. Once or twice in the thread is fine.
- ❌ **Don't compare yourself to specific projects in HN comments** unless someone explicitly asks. Link to differentiation page; let them read it.
- ❌ **Don't claim things the code can't do**. Anything you claim, an alpha tester or commenter will verify within an hour.
- ❌ **Don't hide weak spots**. Mention "early, ~10 stars" yourself. People respect honesty more than polish.

---

## After 24h

- [ ] Tally:
  - Final HN points
  - Final position (front page rank if it landed)
  - GitHub stars added (`gh api repos/<you>/codevira | jq .stargazers_count`)
  - PyPI downloads in the day (`pip-stats codevira` or check pepy.tech)
  - GitHub issues opened
  - DM volume

- [ ] Hot-fix release if any obvious bug surfaced
  ```bash
  # Fix → commit → tag v2.0.1 → push to PyPI
  ```

- [ ] **Write up the 1-page post-mortem** — what worked, what didn't, what you'd do differently. Save to `docs/launch-postmortem.md`.

---

## If it goes well (≥ 50 points, front page)

- [ ] Don't burn out on Day 2 trying to ride the wave
- [ ] Keep replying for 48h max, then taper
- [ ] Channel new GitHub interest into a v2.1 roadmap based on actual feedback

## If it doesn't go well (< 20 points, no front page)

- [ ] Don't relaunch a week later — that's frowned upon
- [ ] Debug: was the title bad? Was the timing wrong? Was the install path broken?
- [ ] Try Lobsters, Tildes, dev.to — different audiences, gentler reception
- [ ] Pivot to the 1:1 outreach grind — DM every developer-friend who'd genuinely want this

---

## Resources

- HN guidelines: https://news.ycombinator.com/showhn.html
- HN best-practice analysis: https://lifehacker.com/show-hn-five-tips-for-launching-on-hacker-news (and similar)
- Pat Walls' indie launch checklist (different products, same dynamics)

---

## After launch

- [ ] If launch went well → tag v2.0.1 with hot-fixes for whatever surfaced
- [ ] If launch went badly → relaunch is OK 2-3 months later WITH MEANINGFUL CHANGES (a "bugfix" relaunch is annoying)
- [ ] Either way: turn the HN feedback into a real v2.1 roadmap, not vapor commitments

The launch is a moment. The product is the long game.
