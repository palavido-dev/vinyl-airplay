# Contributing

Thanks for considering contributing to Vinyl Streamer. This is a personal
project that's grown a bit, and outside contributions are welcome.

## Branches and pull requests

- `main` is what runs on Pis in the wild. Keep it deployable.
- Work on branches named `feat/<slug>`, `fix/<slug>`, or `docs/<slug>`.
- Open a PR against `main`. Include a short description of what changed
  and why, and any hardware you tested on.

If you don't have push access and want it, open an issue or ping me on
a PR. I'd rather hand out collaborator access than play merge ping-pong
on forks.

## Using AI to write code

Using AI assistants (Claude, Copilot, Cursor, etc.) to write code for
this project is fine and encouraged. The bar is just that the code you
submit is:

- **Efficient.** This runs on a Raspberry Pi sharing CPU with audio
  capture, MP3/FLAC encoding, and a web UI. Hot paths (the audio
  callback, the recording loop, the broadcast fanout) are not the place
  for clever-but-expensive code.
- **Clean.** Reads naturally, matches the surrounding style, doesn't
  drag in ten layers of abstraction for a fifty line feature.
- **Dependency-conservative.** Prefer the Python standard library or
  packages already in `requirements.txt`. If you need a new dependency,
  it should be widely used, actively maintained, and open source (PyPI
  download stats and a recent release in the last year are good
  signals). No abandoned packages, no GPL where MIT/BSD/Apache will do,
  no closed-source SDKs.

You own the code you submit regardless of whether you typed it or an
AI did. Read it, run it, and make sure it does what you think it does
before opening the PR.

## Testing your changes

There's no formal test suite yet. Until there is, please confirm in
the PR description:

- The Pi service still starts cleanly (`systemctl status vinyl-streamer`).
- The web UI loads at `http://<pi>:8080/` without 500s.
- If you touched audio capture, recording, or streaming: you actually
  played a record through your change end to end.

Logs live in `journalctl -u vinyl-streamer -f`. Mention any new warnings
or errors that show up.

## Style

- Python: follow the surrounding style. Roughly PEP 8, four-space
  indents, type hints on new public functions.
- Frontend: the UI is server-rendered Jinja2 templates with vanilla
  JS. Keep it that way unless there's a good reason not to. No build
  step.
- Writing (comments, commit messages, PRs, docs): plain prose. No em
  dashes (`—`), no double hyphens (`--`) standing in for em dashes,
  no AI tells like "I've gone ahead and..." or "Generated with...".
  A comma, colon, or period usually does the job.

## Commit messages

Short imperative subject, one blank line, then the why if it's not
obvious from the diff. Reference issues with `#NN`.

```
Fix install.sh skipping user creation on fresh install

UPDATE_MODE always contains a non-empty string, so [[ ! $UPDATE_MODE ]]
was always false and the user-creation branch never ran. Closes #17.
```

No `Co-Authored-By` lines pointing at an LLM, no "Generated with
Claude Code" footers.

## Hardware reality check

This project assumes a Raspberry Pi (4 or 5), a USB or I2S audio input,
and ALSA. PRs that only make sense on a desktop Linux box (PipeWire-only
features, x86-specific binaries, GPU acceleration) probably aren't a
fit. If you're not sure, ask in an issue first.

## Licensing

A formal `LICENSE` file is being added. Until then, by opening a pull
request you agree your contribution is offered under whatever license
the project settles on (expected to be a permissive license such as
MIT or Apache 2.0). No separate CLA.

## Questions

Open a [Discussion](https://github.com/palavido-dev/vinyl-airplay/discussions)
for design questions, an [Issue](https://github.com/palavido-dev/vinyl-airplay/issues)
for bugs.
