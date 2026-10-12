"""The offline evaluation gate: corpora x transports x modes, zero misfiles or it fails.

A *cell* runs one corpus through one transport in one mode, entirely offline:

  transports  memory       in-memory mailbox and drive behind the ports
              http         the real Gmail and Drive adapters, allowlist guard, retries and
                           OAuth, against the simulated Google REST API
              http-faults  the same, with throttling (503) and lost responses injected
  modes       live         run until nothing is deferred; score every document
              preview      a dry run: must write nothing, change no state, and agree with live
              crash        kill the process at every write (before it lands, and after it
                           landed but before the reply), restart, run to the end; the result
                           must be identical to an uninterrupted run

Scoring is strict. A *misfile* is any document filed (outside the review folder) anywhere
other than its expected path, including documents that should have gone to review, been
skipped, or been recognised as duplicates, and any stray file in the drive. Everything else
that differs from the expectation (an unnecessary review, a wrong reason, a lost document) is
a *mismatch*. Floor *violations* cover what must never happen at all: mailbox writes, drive
writes outside the filing root, a preview that writes, conflicts or orphans in the ledger, an
owner's file changed. A cell passes only with zero of all three.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..app import google_stack
from ..classify import LLMClassifier, RuleClassifier, llm_allowlist
from ..defaults import DEFAULT_TYPES, number_patterns
from ..documents import Expander
from ..google.drive import GoogleDrive
from ..google.gmail import GmailMailbox
from ..guard import GuardedTransport
from ..http import RetryingTransport
from ..ledger import Ledger
from ..models import Outcome, OutcomeKind
from ..pipeline import Mailroom
from ..routing import Policy
from ..sim.faults import CrashPlan, SimulatedCrash
from ..sim.google_api import Faults, SimulatedGoogle
from ..sim.memory import InMemoryDrive, InMemoryMailbox
from ..sim.model import SimulatedModel
from ..split import NumberSplitter
from .corpora import CORPUS_NAMES, REVIEW, Corpus, Expected, get_corpus

TRANSPORTS = ("memory", "http", "http-faults")
MODES = ("live", "preview", "crash")
ROOT = "filing-root"
ENDPOINT = "https://llm.example.com/v1/chat/completions"
MAX_RUNS = 5


def _no_sleep(seconds: float) -> None:
    return None


# --- scoring -------------------------------------------------------------------------------

def _describe(exp: Expected) -> str:
    if exp.kind == "filed":
        return f"filed at {exp.path!r}"
    if exp.kind == "review":
        return f"review ({exp.reason})"
    return f"{exp.kind} ({exp.reason})" if exp.reason else exp.kind


def score(corpus: Corpus, outcomes: Mapping[str, Outcome],
          tree: set[str]) -> tuple[list[str], list[str]]:
    """(misfiles, mismatches) of final outcomes and drive contents against the corpus."""
    misfiles: list[str] = []
    mismatches: list[str] = []
    for key, exp in corpus.expected.items():
        got = outcomes.get(key)
        if got is None:
            mismatches.append(f"{key}: no outcome, expected {_describe(exp)}")
            continue
        if got.kind is OutcomeKind.FILED and (exp.kind != "filed" or got.path != exp.path):
            misfiles.append(f"{key}: filed at {got.path!r}, expected {_describe(exp)}")
            continue
        if got.kind.value != exp.kind:
            mismatches.append(f"{key}: {got.kind.value} ({got.reason}), "
                              f"expected {_describe(exp)}")
        elif exp.path and got.path != exp.path:
            mismatches.append(f"{key}: at {got.path!r}, expected {exp.path!r}")
        elif exp.reason and got.reason != exp.reason:
            mismatches.append(f"{key}: reason {got.reason!r}, expected {exp.reason!r}")
    for key, got in outcomes.items():
        if key not in corpus.expected:
            target = misfiles if got.kind is OutcomeKind.FILED else mismatches
            target.append(f"{key}: unexpected document ({got.kind.value} {got.path})".rstrip())
    expected_tree = {e.path for e in corpus.expected.values() if e.kind in ("filed", "review")}
    expected_tree |= {path for path, _ in corpus.existing}
    for path in sorted(tree - expected_tree):
        target = mismatches if path.startswith(REVIEW + "/") else misfiles
        target.append(f"stray file on the drive: {path!r}")
    for path in sorted(expected_tree - tree):
        mismatches.append(f"missing from the drive: {path!r}")
    return misfiles, mismatches


def floor_violations(*, writes_outside_root: list[str], mailbox_writes: list[str],
                     preview_writes: int, bad_ledger_rows: list[str],
                     changed_owner_files: list[str]) -> list[str]:
    out = [f"drive write outside the filing root: {w}" for w in writes_outside_root]
    out += [f"mailbox was asked to change something: {w}" for w in mailbox_writes]
    if preview_writes:
        out.append(f"preview wrote to the drive {preview_writes} time(s)")
    out += [f"ledger: {row}" for row in bad_ledger_rows]
    out += [f"owner's file changed or gone: {path}" for path in changed_owner_files]
    return out


# --- one world: a corpus behind one transport ---------------------------------------------

class World:
    def __init__(self, corpus: Corpus, transport: str,
                 policy_overrides: Mapping[str, Any] | None = None) -> None:
        if transport not in TRANSPORTS:
            raise ValueError(f"unknown transport {transport!r}")
        self.corpus = corpus
        self.transport = transport
        self.policy = Policy(types=DEFAULT_TYPES, **dict(policy_overrides or {}))
        self.model = SimulatedModel(corpus.script)
        self.sim: SimulatedGoogle | None = None
        if transport == "memory":
            self.drive_state = InMemoryDrive(ROOT)
            self._mailbox = InMemoryMailbox(corpus.messages)
        else:
            faults = (Faults(transient_every=7, lose_write_response_every=3)
                      if transport == "http-faults" else None)
            self.sim = SimulatedGoogle(corpus.messages, root_id=ROOT, faults=faults)
            self.drive_state = self.sim.drive
        self.owner_files: dict[str, str] = {}
        for path, data in corpus.existing:
            self._add_owner_file(path, data)
        self.ledger = Ledger(":memory:", root_id=ROOT)
        self.guards: list[GuardedTransport] = []  # one per (re)start, for the audit trail
        self.build()

    def _add_owner_file(self, path: str, data: bytes) -> None:
        *folders, name = path.split("/")
        parent = ROOT
        for folder in folders:
            found = self.drive_state.find_children(parent, folder, folders_only=True)
            parent = found[0].id if found else self.drive_state.add_folder(folder, parent)
        self.drive_state.add_file(name, parent, data)
        self.owner_files[path] = self.drive_state.tree()[path]

    def build(self) -> None:
        if self.sim is None:
            mailbox, drive = self._mailbox, self.drive_state
        else:
            transport, guard = google_stack(self.sim, self.ledger.managed_folder,
                                            credentials=self.sim.credentials, sleep=_no_sleep)
            self.guards.append(guard)
            mailbox = GmailMailbox(transport, page_size=4)
            drive = GoogleDrive(transport, page_size=4)
        types = list(DEFAULT_TYPES.values())
        llm = LLMClassifier(
            RetryingTransport(GuardedTransport(self.model, llm_allowlist(ENDPOINT)),
                              sleep=_no_sleep),
            endpoint=ENDPOINT, model="simulated", api_key="offline", types=types)
        self.mailroom = Mailroom(
            mailbox=mailbox, drive=drive, ledger=self.ledger, classifier=llm,
            policy=self.policy,
            expander=Expander(splitter=NumberSplitter(number_patterns(DEFAULT_TYPES))),
            rules=RuleClassifier(types, vendors=self.corpus.vendors), max_attempts=3)

    def restart(self) -> None:
        """The process died. What the ledger committed survives (the same SQLite database);
        every object in memory is built again, as a new process would."""
        self.ledger = Ledger(root_id=ROOT, _conn=self.ledger.conn)
        self.build()

    def settle(self) -> int:
        """Run until nothing is deferred (a scheduler would keep calling run())."""
        for runs in range(1, MAX_RUNS + 1):
            report = self.mailroom.run()
            if not report.errors and all(o.kind is not OutcomeKind.DEFERRED
                                         for o in report.outcomes):
                return runs
        return MAX_RUNS

    def outcomes(self) -> dict[str, Outcome]:
        return {o.key: o for o in self.ledger.outcomes()}

    def tree(self) -> set[str]:
        return set(self.drive_state.tree())

    def floor(self, *, preview: bool = False) -> list[str]:
        outside = [f"{op} {name!r} into {parent}" for op, parent, name in self.drive_state.ops
                   if self.drive_state.path_of(parent) is None]
        mailbox_writes: list[str] = []
        if self.sim is not None:
            mailbox_writes = [f"{m} {u}" for m, u in self.sim.requests
                              if "gmail.googleapis.com" in u and m != "GET"]
            mailbox_writes += [f"unexpected request {m} {u}" for m, u in self.sim.unexpected]
        rows = self.ledger.conn.execute(
            "SELECT status, name FROM writes WHERE status IN ('intent', 'conflict', 'orphan')")
        tree = self.drive_state.tree()
        changed = [p for p, digest in self.owner_files.items() if tree.get(p) != digest]
        return floor_violations(
            writes_outside_root=outside, mailbox_writes=mailbox_writes,
            preview_writes=self.drive_state.write_attempts if preview else 0,
            bad_ledger_rows=[f"{r['status']} write left for {r['name']!r}" for r in rows],
            changed_owner_files=changed)


# --- cells ---------------------------------------------------------------------------------

@dataclass
class CellResult:
    corpus: str
    transport: str
    mode: str
    documents: int = 0
    filed: int = 0
    reviewed: int = 0
    runs: int = 0
    crash_points: int = 0
    seconds: float = 0.0
    misfiles: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not (self.misfiles or self.mismatches or self.violations)

    def problems(self) -> str:
        lines = [f"{self.corpus}/{self.transport}/{self.mode}:"]
        for label, items in (("MISFILE", self.misfiles), ("mismatch", self.mismatches),
                             ("VIOLATION", self.violations)):
            lines += [f"  {label}: {item}" for item in items]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "corpus", "transport", "mode", "documents", "filed", "reviewed", "runs",
            "crash_points", "seconds", "misfiles", "mismatches", "violations")} | {
            "passed": self.passed}


def _signature(outcomes: Mapping[str, Outcome]) -> dict[str, tuple[str, str, str]]:
    return {k: (o.kind.value, o.path, o.reason) for k, o in outcomes.items()}


def _live(cell: CellResult, corpus: Corpus, transport: str, overrides) -> World:
    world = World(corpus, transport, overrides)
    cell.runs = world.settle()
    outcomes = world.outcomes()
    cell.misfiles, cell.mismatches = score(corpus, outcomes, world.tree())
    cell.violations = world.floor()
    cell.filed = sum(o.kind is OutcomeKind.FILED for o in outcomes.values())
    cell.reviewed = sum(o.kind is OutcomeKind.REVIEW for o in outcomes.values())
    return world


def _preview(cell: CellResult, corpus: Corpus, transport: str, overrides) -> None:
    world = World(corpus, transport, overrides)
    preview = world.mailroom.run(preview=True)
    cell.runs = 1
    cell.violations = world.floor(preview=True)
    if world.ledger.outcomes() or world.ledger.conn.execute(
            "SELECT count(*) FROM messages").fetchone()[0]:
        cell.violations.append("preview changed the ledger")
    live = World(corpus, transport, overrides).mailroom.run()
    seen = _signature({o.key: o for o in preview.outcomes})
    expected = _signature({o.key: o for o in live.outcomes})
    for key in sorted(set(seen) | set(expected)):
        if seen.get(key) != expected.get(key):
            cell.mismatches.append(f"{key}: preview says {seen.get(key)}, "
                                   f"a live run does {expected.get(key)}")
    cell.filed = sum(o.kind is OutcomeKind.FILED for o in preview.outcomes)
    cell.reviewed = sum(o.kind is OutcomeKind.REVIEW for o in preview.outcomes)


def _crash(cell: CellResult, corpus: Corpus, transport: str, overrides, stride: int) -> None:
    clean = World(corpus, transport, overrides)
    clean.settle()
    want_outcomes, want_tree = _signature(clean.outcomes()), clean.tree()
    cell.filed = sum(o.kind is OutcomeKind.FILED for o in clean.outcomes().values())
    cell.reviewed = sum(o.kind is OutcomeKind.REVIEW for o in clean.outcomes().values())
    # every write attempt can die before it lands; only those that landed can die after
    attempts = range(clean.drive_state.write_attempts)
    points = sorted([(k, "before") for k in attempts[::stride]]
                    + [(k, "after") for k in clean.drive_state.landed_attempts[::stride]])
    for index, when in points:
        world = World(corpus, transport, overrides)
        world.drive_state.crash = CrashPlan(at=index, when=when)
        try:
            world.settle()
            cell.violations.append(f"crash {when} write {index} was never reached")
            continue
        except SimulatedCrash:
            pass
        world.drive_state.crash = None
        world.restart()
        cell.runs += world.settle()
        label = f"crash {when} write {index}"
        got = world.outcomes()
        misfiles, mismatches = score(corpus, got, world.tree())
        cell.misfiles += [f"{label}: {m}" for m in misfiles]
        cell.mismatches += [f"{label}: {m}" for m in mismatches]
        if _signature(got) != want_outcomes or world.tree() != want_tree:
            cell.mismatches.append(f"{label}: result differs from an uninterrupted run")
        cell.violations += [f"{label}: {v}" for v in world.floor()]
    cell.crash_points = len(points)


def run_cell(corpus_name: str, transport: str, mode: str, *,
             policy_overrides: Mapping[str, Any] | None = None,
             crash_stride: int = 1) -> CellResult:
    corpus = get_corpus(corpus_name)
    cell = CellResult(corpus_name, transport, mode, documents=len(corpus.expected))
    started = time.perf_counter()
    if mode == "live":
        _live(cell, corpus, transport, policy_overrides)
    elif mode == "preview":
        _preview(cell, corpus, transport, policy_overrides)
    elif mode == "crash":
        _crash(cell, corpus, transport, policy_overrides, crash_stride)
    else:
        raise ValueError(f"unknown mode {mode!r}")
    cell.seconds = round(time.perf_counter() - started, 3)
    return cell


# --- the whole gate ------------------------------------------------------------------------

@dataclass(frozen=True)
class GateOptions:
    corpora: tuple[str, ...] = CORPUS_NAMES
    transports: tuple[str, ...] = TRANSPORTS
    modes: tuple[str, ...] = MODES
    crash_stride: int = 1
    policy_overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class GateReport:
    cells: list[CellResult]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cells)

    def totals(self) -> dict[str, int]:
        return {"cells": len(self.cells),
                "documents": sum(c.documents for c in self.cells),
                "crash_points": sum(c.crash_points for c in self.cells),
                "misfiles": sum(len(c.misfiles) for c in self.cells),
                "mismatches": sum(len(c.mismatches) for c in self.cells),
                "violations": sum(len(c.violations) for c in self.cells)}

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "totals": self.totals(),
                "cells": [c.to_dict() for c in self.cells]}

    def render(self) -> str:
        head = (f"{'corpus':<12} {'transport':<12} {'mode':<8} {'docs':>5} {'filed':>6} "
                f"{'review':>7} {'crashes':>8} {'misfiles':>9} {'other':>6} {'secs':>6}  result")
        lines = [head, "-" * len(head)]
        for c in self.cells:
            lines.append(
                f"{c.corpus:<12} {c.transport:<12} {c.mode:<8} {c.documents:>5} {c.filed:>6} "
                f"{c.reviewed:>7} {c.crash_points:>8} {len(c.misfiles):>9} "
                f"{len(c.mismatches) + len(c.violations):>6} {c.seconds:>6.2f}  "
                f"{'PASS' if c.passed else 'FAIL'}")
        totals = self.totals()
        lines.append("-" * len(head))
        lines.append(f"{totals['cells']} cells, {totals['crash_points']} crash points, "
                     f"{totals['misfiles']} misfiles, {totals['mismatches']} mismatches, "
                     f"{totals['violations']} violations: "
                     f"{'GATE PASSED' if self.passed else 'GATE FAILED'}")
        for c in self.cells:
            if not c.passed:
                lines.append(c.problems())
        return "\n".join(lines)


def run_gate(options: GateOptions | None = None) -> GateReport:
    options = options or GateOptions()
    cells = [run_cell(corpus, transport, mode, policy_overrides=options.policy_overrides,
                      crash_stride=options.crash_stride)
             for corpus in options.corpora for transport in options.transports
             for mode in options.modes]
    return GateReport(cells)
