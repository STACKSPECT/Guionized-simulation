# Contributing

Thanks for looking. This is a small, opinionated repository: a **scripted** palletizing
simulation whose job is to feed real measurements to the observability platform. Before
changing anything, read [`AGENTS.md`](AGENTS.md) — it carries the hard rules and the
reasoning behind every odd-looking value in `configs/`. If something there contradicts
what you expect, that document wins; if it is out of date, fix it in the same PR.

## Getting set up

```bash
git clone --branch dev https://github.com/STACKSPECT/Platform.git ../Platform
SDK=../Platform/backend bash scripts/setup.sh
source .venv/bin/activate
```

`scripts/setup.sh` is idempotent — re-run it whenever `requirements.txt` changes. It
builds `.venv/`, installs the pinned dependencies, installs the `theker_telemetry` SDK
editable from the sibling `Platform` checkout, and sparse-clones the Franka Panda model
into `third_party/`.

**The SDK is not optional.** It is not on PyPI and it is imported at module level in the
scripts, in `src/pallet/` and in the tests, so without it nothing runs — not even the test
suite. The README's [requirements section](README.md#the-one-dependency-that-is-not-in-requirementstxt)
explains why it lives in the other repository.

Supabase credentials are optional. Without a `.env` the simulation runs and writes to
`runs/`; with one it also uploads. Copy [`.env.example`](.env.example) if you need it, and
never commit the result.

## Branches

`dev` is the integration branch. Branch from it, and open your pull request **against
`dev`** — not `main`.

```bash
git switch dev && git pull
git switch -c my-change
```

`main` is the released state and is not pushed to directly.

## Commits

The history follows [Conventional Commits](https://www.conventionalcommits.org/), with an
optional scope:

```
feat(telemetry): sube la cenital y el alzado a Supabase Storage
fix(cli): --telemetry sin Supabase falla en voz alta
docs: guia de integracion al dia con el contrato de dev
```

Types in use so far: `feat`, `fix`, `docs`, `refactor`, `chore`. Subjects have been
written in Spanish, matching the code's comments — keep whichever language the
surrounding history uses rather than mixing within a series.

Describe the change's *effect*, not the files you touched. Please do not add
`Co-Authored-By` trailers.

## Running the checks

There is no CI to lean on yet, so run these before opening a PR:

```bash
python tests/test_pallet.py      # 16 checks
python -m src.pallet.measure     # the measurement's own self-check
```

And for anything touching the manoeuvre, the scene or the measurement, run a real episode
and look at the result:

```bash
python scripts/palletize.py -n 1 --no-telemetry
```

It takes about three minutes and writes `runs/<timestamp>-pallet/<seed>/009-top.png` and
`009-side.png`. Open them. A pallet that measures well but looks wrong is still wrong.

## Writing tests

**The tests deliberately use no framework.** They are plain `assert`s in functions named
`test_*`, collected and run by `main()` at the bottom of the file when you execute it as a
script. There is no pytest, no fixtures, no conftest, and they do not start the simulator
— which is why the whole suite runs in under a second.

New tests follow that. Add a `test_*` function to `tests/test_pallet.py`; `main()` picks it
up automatically by name. Name it after the property it protects, in the style already
there (`test_the_cog_counts_boxes_outside_tolerance`), so a failure reads as a sentence
about what broke.

If a value in `configs/` was hard to find, anchor it with a test rather than a comment
alone — several of the existing checks exist precisely because a number got tuned by eye
once and cost an afternoon.

## Scope

A few things are out of bounds by design, and [`AGENTS.md`](AGENTS.md) §6 has the full
list. The short version:

- **No decision logic in the script.** If it starts choosing, it stops being this
  simulation, and that needs saying out loud rather than sliding in.
- **No duplicating the platform's schema.** `src/pallet/telemetry.py` is the only boundary;
  the rest of the code should not know a single column name.
- **No tuning `configs/` by eye.** Every strange value has its measurement beside it.
- **No new dependencies** without first checking whether MuJoCo or numpy already do it.

## Reporting problems

Open an issue with what you ran, what you expected, and what happened. For a failed
episode, the useful attachments are `runs/<timestamp>-pallet/episodes.jsonl` and the two
PNGs of the run — between them they usually make the cause obvious.
