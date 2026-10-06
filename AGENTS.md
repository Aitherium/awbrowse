# awbrowse for agents

Read this if you are an agent (or a human) editing this package. Short on
purpose: the commands, the traps that cost a session, and where the rest lives.
Nothing here is read at runtime — it is for you.

## What this is

PyPI distribution **`awbrowse`** (version in `pyproject.toml`), import package
`awbrowse`, Python >= 3.10. A portable browser client — navigate, console,
network, DOM, screenshot. Handing an agent a screenshot makes it read a
picture of text; handing it raw DOM makes it read a megabyte of markup.
Neither of those is the page.

This repository is a **synced mirror** of the AitherOS monorepo (lane
`.github/workflows/sync-awbrowse.yml`). Hand edits made here are overwritten on
the next sync — change the source and let the lane publish.

## Build, test, verify

```bash
python -m pytest tests -q        # the suite: 41 tests, green at v0.1.0
pip install -e .                 # editable install for developing against it
```

The suite was run from a source checkout with no prior install. The publish
lane (`publish-brick.yml`) additionally builds the wheel, installs it and
imports it — a tree that tests green can still ship a broken wheel.

## Rules that keep this useful

- **The client and the driver are different products, and both are tested.**
  `test_client.py` pins the client contract; `test_obscura.py` pins the
  rendering path. A change to one that quietly changes the other fails there.
- **What the agent receives IS the product.** Text, console, network and the
  screenshot each answer a different question; never widen one of them into
  "here is everything" — a browser tool that returns the whole page is a
  token fire, and one that returns only pixels is a summariser.
- **Config is tested against the linter it shares.** `test_lint_config_agrees.py`
  exists because two lists that claim the same thing is this family's recurring
  defect shape. Config edits land with that test still green.
- **The registry drives the public surface.** This repo's README header,
  `llms.txt` and `aither-manifest.json` are generated from the ecosystem
  registry (one yaml in the AitherOS monorepo) and rewritten on every sync.
  Change the registry; do not hand-edit the generated blocks.

## Read next

- `llms.txt` — the install/use card written for an agent to execute
- `README.md` — the human front door
- `docs/` — the generated docs site source
