# Alpha tester invite drafts

Five message templates, copy-paste ready. Pick the one that fits each person you're pinging. Edit the `[brackets]` before sending.

The goal of v2.0-rc.1 alpha is **3 testers running for ≥2 days each** with at least one switching between AI tools mid-project. After 3 testers complete the run with no critical bugs, tag v2.0.0 GA + HN submit.

---

## Template 1 — DM to a stargazer / GitHub follower

> Hey [name] — saw you starred codevira a while back. We're shipping v2.0 (10 new "AI guardian" capabilities + universality wedge across all AI coding tools) and looking for 3 alpha testers. Would you be up for ~2 days of running it on a real project? The whole thing is `pipx install codevira && codevira setup` and then it works in the background.
>
> 30s demo: [link to docs/demo/codevira-demo.mp4]
>
> What you'd get: free pre-release. What I'd ask: a 1-page note at the end on what worked / what bugged you / 1 thing you'd change.
>
> No commitment if you can't — totally fine. Just figured I'd ask first.

---

## Template 2 — Reply to a relevant HN thread

(Use ONLY when there's an organic match — never spam threads.)

> Just shipping v2.0 of a tool that does [the specific thing the thread is about — paste the relevant 1 sentence from your README]. Looking for 3 alpha testers before I post a Show HN. If you're working on [the thing], DM me and I'll send the install one-liner + a 30s demo.
>
> Local-first, MIT, no signup.

---

## Template 3 — Twitter / X reply

> Working on this exact problem from the other side: codevira ships [single-most-relevant feature for this convo]. Looking for alpha testers — DM if interested. Local, MIT, `pipx install` install.

---

## Template 4 — Email to a developer-friend

Subject: `quick favor — alpha test something I built?`

> Hey [name],
>
> I've been heads-down for the past few months on codevira — a memory layer for AI coding agents that works across every tool you use (Claude Code, Cursor, Windsurf, Antigravity). v2.0 ships 10 "AI guardian" capabilities that intercept every Edit / prompt the AI tries and warn / block when it's about to do something inconsistent with your project's history.
>
> Quick demo: [link to codevira-demo.mp4 — 30 seconds]
> Differentiation: [link to docs/vs-other-memory-tools.md]
>
> Looking for 3 alpha testers before public launch. Asking takes ~2 days of normal coding (no extra time required — codevira runs in the background) and ends with a 1-page note from you on what worked / what didn't.
>
> If you're up for it, the install is:
>
> ```
> pipx install codevira && codevira setup
> ```
>
> No worries if not — I know you're busy. Either way, would love your honest reaction to the 30s demo even if you can't test.
>
> Thanks,
> [your name]

---

## Template 5 — Slack DM (developer Slacks where you're a member)

> hey 👋 quick alpha-tester ask — i've been building a memory layer for AI coding agents (works across Claude Code, Cursor, Windsurf, Antigravity simultaneously, decision protection, scope locks). shipping v2.0 next week and need 3 testers to do ~2 days of real coding with it before public launch. local-first, MIT, no signup, install is one command. 30s demo: [link]. dm if interested!

---

## Tester onboarding checklist

When someone says yes, send them this:

> Welcome 🙏. Here's the 5-min onboarding:
>
> 1. Install:
>    ```
>    pipx install codevira
>    cd ~/your-real-project
>    codevira setup
>    codevira doctor   # should be all ✓ or only minor ⚠
>    ```
>
> 2. Use codevira normally for ≥ 2 days. Don't change anything; let it run in the background.
>
> 3. Try at least 3 of these scenarios sometime during the 2 days:
>    - Ask the AI: "what did I decide about [some-thing] last week?"
>    - Switch between Claude Code → Cursor → ask the same question (the wedge moment)
>    - Run `codevira insights` after day 2
>    - Run `codevira replay` after day 2
>
> 4. After 2 days, send me back ANY of:
>    - 1-paragraph answer to "did codevira save you time? would you keep it?"
>    - Crash logs if you hit any (`codevira report`)
>    - Things that bugged you / wording you'd change
>    - Things you wanted that don't exist
>
> No long forms; just be honest. If something completely broke, just tell me what you were doing when it broke — I'll fix it within hours.

---

## Tracking spreadsheet (or txt note)

Keep a 1-line tracker:

```
Tester     | Pinged    | Replied | Installed | Started   | Day 1 | Day 2 | Wrap-up | Notes
-----------|-----------|---------|-----------|-----------|-------|-------|---------|------
[name1]    | 2026-05-06| ✓       | 2026-05-07| 2026-05-07|       |       |         |
[name2]    |           |         |           |           |       |       |         |
[name3]    |           |         |           |           |       |       |         |
```

When you have 3 in the "Wrap-up" column, decide ship / don't ship per the DOGFOOD.md decision rule.

---

## Reasons NOT to ship to HN immediately after rc.1 alpha

- Wedge broke for any tester (the universality promise is the headline)
- Anyone hit a crash that required `pipx uninstall codevira && pipx install codevira` to recover
- 2+ testers reported the same friction issue (signal that the OOTB experience is wrong)
- You yourself wouldn't recommend it to someone you respect

If any of these → fix → re-test on the same testers (don't recruit fresh) → re-evaluate.
