# Contributing to Gatekeeper

Thanks for helping. Bug reports, ideas, docs fixes and code are all welcome.

- **Bugs and ideas:** open an issue. For a bug, include the relevant log lines (every line
  carries the visit's Frigate event id) and your setup: compose stack or HA add-on, camera,
  Frigate version.
- **Larger changes:** open an issue first to agree on the approach before you write the
  code.
- **The Home Assistant add-on** (packaging, add-on options, its docs) lives in
  [Gatekeeper-HA](https://github.com/Gatekeeper-HA/Gatekeeper-HA). The code it runs is this
  repository's `gatekeeper` package.

## Contributor License Agreement

Before we can merge your first pull request, you need to agree to the
[Contributor License Agreement](CLA.md). A bot comments on the pull request with
instructions; agreeing takes one comment.

In short:
- You keep the copyright in your work.
- You give Lobo Dorado LLC, which maintains Gatekeeper, a broad license to it. That lets the
  project also be offered under other terms, such as a commercial license for companies that
  build Gatekeeper into their products.
- We promise that any version including your work is also available under an open-source
  license.

The [CLA](CLA.md) itself is what counts.

**AI-assisted contributions are welcome.** Review what the tool wrote as if it were your own,
and say in the pull request which parts substantially came from an AI tool.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"     # core + test tools; no torch or aiortc needed
ruff check src tests
pytest --cov
```

The speech and talkback dependencies are optional extras, imported lazily, so the core logic
is tested without them. See [Development](README.md#development) in the README for the
Docker image and the dependency lock file.

## Pull requests

- Branch from `main` and open a pull request against it. `main` is protected: CI (ruff,
  pytest with coverage, and a compose config check) must pass before merging.
- Add or update tests for any change in behavior.
- Fill in the template's *Live check* (see below).
- Update the README for changed options or behavior. Add a line under *Unreleased* in
  [CHANGELOG.md](CHANGELOG.md).
- Keep each pull request to one change. Small ones get reviewed faster.

## The daily report

A merge to `main` goes live on the project's rig within minutes. Its daily report
(`src/gatekeeper/report.py`, README "Running a rig") is how a change is seen working on a
real porch, and it's what the reviewer reads to find bugs. So a change that adds a feature
or fixes a bug also adds the check or the number that shows it working, with a test in
`tests/test_report.py`. For example:
- a new kind of visitor: the report already lists every visit's classification, so say so;
- a fix for a slow reply: add the reply time to the report, and a check against a limit;
- a new log line that means trouble: nothing to add, since errors and warnings are
  counted and listed by kind. Add a harmless one to `NOISE`.

Docs, tests and CI changes need nothing live; say so in the template.
