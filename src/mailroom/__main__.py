"""mailroom command line.

    mailroom run --config mailroom.toml [--env-file .env] [--preview] [--json]
                                                                file new mail from Gmail
    mailroom recover --config mailroom.toml                     settle writes left by a crash
    mailroom eval [--corpus C] [--transport T] [--mode M]       the offline evaluation gate
    mailroom demo [--corpus C] [--json]                         a full offline run
    mailroom allowlist                                          every request mailroom may make
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .classify import llm_allowlist
from .config import ConfigError, Settings
from .google.auth import AuthError
from .google.policy import google_allowlist
from .guard import BlockedRequest
from .http import HttpError, TransportError
from .report import render_allowlist, render_counts, render_outcomes, render_run

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_BLOCKED = 0, 1, 2, 3


def _parser() -> argparse.ArgumentParser:
    from .evaluation.corpora import CORPUS_NAMES
    from .evaluation.gate import MODES, TRANSPORTS

    parser = argparse.ArgumentParser(prog="mailroom", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="file new mail (add --preview for a dry run)")
    run.add_argument("--config", type=Path, default=Path("mailroom.toml"))
    run.add_argument("--preview", action="store_true", help="decide everything, write nothing")
    run.add_argument("--json", action="store_true", help="print the report as JSON")
    recover = sub.add_parser("recover", help="settle filings a crash left unfinished")
    recover.add_argument("--config", type=Path, default=Path("mailroom.toml"))
    for command in (run, recover):
        command.add_argument("--env-file", type=Path,
                             help="KEY=VALUE file (e.g. .env) read before the environment")
    gate = sub.add_parser("eval", help="run the offline evaluation gate")
    gate.add_argument("--corpus", action="append", choices=CORPUS_NAMES)
    gate.add_argument("--transport", action="append", choices=TRANSPORTS)
    gate.add_argument("--mode", action="append", choices=MODES)
    gate.add_argument("--crash-stride", type=int, default=1,
                      help="test every Nth crash point (default: all of them)")
    gate.add_argument("--disable", action="append", default=[],
                      choices=("cross_check", "grounding"),
                      help="switch a safety check off, to watch the gate catch the misfiles")
    gate.add_argument("--json", type=Path, help="also write the full report here")
    demo = sub.add_parser("demo", help="file a synthetic mailbox end to end, offline")
    demo.add_argument("--corpus", choices=CORPUS_NAMES, default="everyday")
    demo.add_argument("--json", action="store_true")
    sub.add_parser("allowlist", help="print every request mailroom is allowed to make")
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "allowlist":
        rows = (google_allowlist(lambda folder_id: True).describe()
                + llm_allowlist(Settings.endpoint).describe())
        print(render_allowlist(rows))
        return EXIT_OK
    if args.command == "eval":
        return _eval(args)
    if args.command == "demo":
        return _demo(args)
    return _live(args, env)


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _live(args: argparse.Namespace, env: Mapping[str, str] | None) -> int:
    from .app import build_app

    env = dict(os.environ if env is None else env)
    try:
        if args.env_file:
            env = {**read_env_file(args.env_file), **env}
        app = build_app(Settings.load(args.config), env=env)
    except (ConfigError, AuthError, OSError) as err:
        print(f"mailroom: {err}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        if args.command == "recover":
            recovery = app.mailroom.recover()
            print(json.dumps(vars(recovery), indent=2))
            return EXIT_OK if recovery.clean else EXIT_FAILED
        report = app.mailroom.run(preview=args.preview)
    except AuthError as err:
        print(f"mailroom: {err}", file=sys.stderr)
        return EXIT_CONFIG
    except BlockedRequest as err:
        print(f"mailroom: stopped, a request was refused by the allowlist: {err}",
              file=sys.stderr)
        return EXIT_BLOCKED
    except (TransportError, HttpError) as err:
        print(f"mailroom: could not reach Google: {err}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        app.close()
    print(json.dumps(report.to_dict(), indent=2) if args.json else render_run(report))
    conflicts = any(o.kind.value == "conflict" for o in report.outcomes)
    return EXIT_FAILED if conflicts or report.errors or not report.recovery.clean else EXIT_OK


def _eval(args: argparse.Namespace) -> int:
    from .evaluation.gate import GateOptions, run_gate

    defaults = GateOptions()
    options = GateOptions(
        corpora=tuple(args.corpus or defaults.corpora),
        transports=tuple(args.transport or defaults.transports),
        modes=tuple(args.mode or defaults.modes), crash_stride=max(1, args.crash_stride),
        policy_overrides={name: False for name in args.disable})
    report = run_gate(options)
    print(report.render())
    if args.json:
        args.json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return EXIT_OK if report.passed else EXIT_FAILED


def _demo(args: argparse.Namespace) -> int:
    from .evaluation.corpora import get_corpus
    from .evaluation.gate import World

    world = World(get_corpus(args.corpus), "http")
    runs = world.settle()
    outcomes = sorted(world.ledger.outcomes(), key=lambda o: o.key)
    requests = Counter(name for guard in world.guards for name, _, _ in guard.audit)
    if args.json:
        print(json.dumps({
            "corpus": args.corpus, "runs": runs,
            "counts": dict(Counter(o.kind.value for o in outcomes)),
            "outcomes": [{"key": o.key, "kind": o.kind.value, "path": o.path,
                          "reason": o.reason} for o in outcomes],
            "drive": sorted(world.tree()), "requests": dict(sorted(requests.items()))},
            indent=2))
        return EXIT_OK
    corpus = world.corpus
    print(f"demo: the '{corpus.name}' corpus ({corpus.description}), filed through the real "
          "Gmail and Drive adapters\nand the allowlist guard against the simulated Google "
          f"APIs and a scripted model. {runs} run(s).\n")
    print("\n".join(render_outcomes(outcomes)))
    print(f"\ncounts: {render_counts(outcomes)}\n\nthe drive afterwards:")
    print("\n".join(f"  {path}" for path in sorted(world.tree())))
    print("\nrequests the allowlist let through, by rule:")
    print("\n".join(f"  {name:<26} {count}" for name, count in sorted(requests.items())))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
