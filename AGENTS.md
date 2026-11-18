# Working on Paper Daily

- Public source code lives here; personal data stays in ignored files. Never add papers, feedback, credentials, local browser profiles or notification receipts to Git.
- Read WORKFLOW.md and the user's local reading_profile.md before producing a daily report. The software does not itself read papers or call an LLM.
- Keep manual `--open` and automatic `--watch` behavior separate. Manual viewing must not acknowledge notifications or stop the watcher.
- Bind the workbench to loopback. Use an isolated browser profile; do not copy personal cookies or sign in to browser sync.
- Check sources and complete media verification before `ready`. A plausible title or successful HTTP response is not evidence of full-text reading.
- Do not overwrite user notes, confirmed journal sentences or local configuration. Do not install another scheduler behind the user's back.
- Default total model concurrency is at most 2, including the main agent. Respect the user's provider limit and avoid adding concurrency when shared occupancy is unknown.
- Run offline tests with `python3 -m unittest discover -p 'test_*.py'` and `python3 -m unittest discover -s prototype_v2 -p 'test_*.py'`.
