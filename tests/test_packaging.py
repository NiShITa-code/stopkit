"""Packaging checks for this repository, run by pytest on every push.

The checks read three things: the text of every tracked file (vocabulary,
register, spelling, citations), the layout of the README and its agreement with
the tree, and the contents and messages of every reachable commit.

The recorded runs in validation/ are shipped as recorded, so the checks on wording
and names skip them. So do the checks of the history, for the records of the
earlier studies, which the repository held in experiments/ up to commit EARLIER:
the manifest that commit holds lists every recorded file with its SHA-256.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = "stopkit"  # the package directory name under src/
SELF = Path(__file__).resolve().relative_to(ROOT).as_posix()
# Paths this file had in earlier commits, if it was ever renamed. The history
# scans skip this file under each of them, as they do under SELF.
SELF_EARLIER_PATHS: tuple[str, ...] = ()
CI = ".github/workflows/ci.yml"
# The last commit that holds the earlier studies, experiments/, which are kept outside the repository since. Its
# experiments/data/MANIFEST.json lists the recorded files, in validation/ and in experiments/, with their SHA-256.
EARLIER = "19544dda0a08ef0e1532735d8505a671609a5959"
# Files the manifest lists under these prefixes are moved code, held to every check.
MOVED_CODE = ("src/", "tests/", "examples/")
# Home folders the records hold that belong to no person (.github/scripts/identity_scan.py lists the same).
ACCOUNTS = frozenset(
    {
        "stopkit-agent",
        "stopkit-sentinel",
        "stopkit-worker",
        "stopkit-test",
        "ocspike",
        ".claude",
        ".codex",
        "runner",
        "u",
    }
)


@functools.lru_cache(maxsize=1)
def manifest() -> dict:
    """The recorded files by path, each with its SHA-256: experiments/data/MANIFEST.json as commit EARLIER holds it."""
    r = subprocess.run(
        ["git", "show", f"{EARLIER}:experiments/data/MANIFEST.json"], cwd=ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0, f"commit {EARLIER[:7]} is not in this checkout; these checks need the full history"
    return json.loads(r.stdout)["files"]


def records() -> set[str]:
    """The recorded files: everything the manifest lists outside the moved code."""
    return {p for p in manifest() if not p.startswith(MOVED_CODE)}


def tracked() -> list[str]:
    """Paths of every file in the git index."""
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, "git ls-files failed; these checks need a git checkout"
    return out.stdout.split()


def prose_files(exclude_self: bool = True) -> dict[str, str]:
    """Text of every tracked file except LICENSE, data files (.csv, .png, .npz, .lock) and the records.

    With exclude_self, this file and the CI workflow are left out as well, since
    both hold the patterns the checks search for.
    """
    out = {}
    recorded = records()
    for rel in tracked():
        if rel == "LICENSE" or rel.endswith((".csv", ".png", ".npz", ".lock")) or rel in recorded:
            continue
        if exclude_self and rel in (SELF, CI):
            continue
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            out[rel] = p.read_text(errors="ignore")
        except OSError:
            continue
    return out


def scan(pattern: str, files: dict[str, str]) -> dict[str, list[str]]:
    """Map each file that matches `pattern`, ignoring case, to its distinct matches."""
    rx = re.compile(pattern, re.I)
    return {f: sorted(set(rx.findall(t))) for f, t in files.items() if rx.search(t)}


# --- local source directory ----------------------------------------------------


def test_the_earlier_studies_are_not_tracked():
    """Nothing under experiments/ is tracked: the earlier studies are kept outside the repository since EARLIER."""
    held = [f for f in tracked() if f.startswith("experiments/")]
    assert not held, f"files under experiments/ are tracked: {held[:5]}"


# --- publication status --------------------------------------------------------


def test_no_release_process_or_review_status_language():
    """No tracked text file states the publication status of the work or the terms of its release."""
    hits = scan(r"anonym|double.blind|under.review|flip.public|review.decision|embargo", prose_files())
    assert not hits, f"publication-status language in tracked files: {hits}"


def test_no_venue_attribution_for_this_work():
    """Every venue named in a Markdown file is also named in a tracked file that is not Markdown.

    A venue cited for prior work is recorded with that work in a shipped record
    that is not Markdown. A venue named only in documentation has no such
    record and fails the check.
    """
    rx = re.compile(r"\b(AAAI|ICLR|NeurIPS|ICML|EACL|ACL|CVPR|ECCV|USENIX|TMLR|ICCV)\b")
    files = prose_files()
    supported = set()
    for f, body in files.items():
        if not f.endswith(".md"):
            supported |= set(rx.findall(body))

    offenders = {}
    for f, body in files.items():
        if not f.endswith(".md"):
            continue
        unsupported = sorted(set(rx.findall(body)) - supported)
        if unsupported:
            offenders[f] = unsupported
    assert not offenders, (
        f"venue named in documentation with no record outside it: {offenders}; record the cited "
        "work in a shipped manifest, or remove the venue"
    )


# --- register ------------------------------------------------------------------


def test_no_adversarial_framing():
    """No tracked text file frames a comparison as a contest; a baseline is called by its name.

    The pattern matches contest wording only. A table of per-cell measurements
    is a comparison and is allowed. "beats" alone is the name of the kit's
    heartbeat oracle (src/stopkit/spec/verdicts.json), so it counts only when
    what it would beat follows.
    """
    contest = r"\brivals?\b|the.win.lives|\bbeats\s+(?:the|all|every|both|each|its|our|their)\b|Pareto.dominates"
    hits = scan(contest, prose_files())
    assert not hits, f"comparison framed as a contest: {hits}"


def test_no_aphorisms_or_self_praise():
    """No tracked text file contains a maxim or a claim about its own candor."""
    hits = scan(
        r"comment,.not.a.gate|is.decoration|not.a.ping|mean.something|what.makes.this.real"
        r"|\bhonest(?:ly)?\b|as.a.stranger|marketing.table|the.whole.(?:theory|crux)",
        prose_files(),
    )
    assert not hits, f"maxim or self-assessment in tracked files: {hits}"


def test_no_gendered_reference_to_the_author():
    """No tracked text file uses a gendered third-person pronoun."""
    hits = scan(r"\b(?:his|her|him|hers)\b", prose_files())
    assert not hits, f"gendered pronoun in tracked files: {hits}"


def test_consistent_spelling_convention():
    """Tracked text uses American spelling; the pattern lists the British forms to reject."""
    hits = scan(
        r"realisable|idealisation|behaviour|labelled|penalised|normalisation|generalises"
        r"|memorised|parameterisation|summarises|analysed",
        prose_files(),
    )
    assert not hits, f"British spelling in tracked files: {hits}"


# --- names ---------------------------------------------------------------------

STRUCTURAL_VOCABULARY = re.compile(
    r"calib|author_plane|author_harness|extract_author|scoreboard|withheld|thesis"
    r"|\btheory\b|\blemma\d?|theorem|corollary|caveat|census|streisand|tdec"
    r"|multiseed|harness_r\d|release_hygiene|_plane\b",
    re.I,
)


def test_no_internal_vocabulary_in_filenames():
    """No tracked path outside the records contains a term matched by STRUCTURAL_VOCABULARY.

    The content checks read the text of each file; this check reads the file
    and directory names, which those checks do not see.
    """
    recorded = records()
    hits = [f for f in tracked() if f not in recorded and STRUCTURAL_VOCABULARY.search(f)]
    assert not hits, f"internal vocabulary in tracked paths: {hits}"


def test_no_internal_vocabulary_in_public_names():
    """Subcommands, HTTP routes and environment variables avoid STRUCTURAL_VOCABULARY.

    Subcommands are read from the `choices=` tuples in cli.py, routes from the
    decorators in app.py, and environment variables from app.py, cli.py and
    results.py in the package.
    """
    offenders = {}

    src = ROOT / "src" / PKG
    cli = (src / "cli.py").read_text() if (src / "cli.py").is_file() else ""
    for group in re.findall(r"choices=\(([^)]*)\)", cli):
        bad = [c for c in re.findall(r'"([^"]+)"', group) if STRUCTURAL_VOCABULARY.search(c)]
        if bad:
            offenders["subcommands"] = bad

    app = (src / "app.py").read_text() if (src / "app.py").is_file() else ""
    bad = [r for r in re.findall(r'@app\.(?:get|post)\("([^"]+)"', app) if STRUCTURAL_VOCABULARY.search(r)]
    if bad:
        offenders["routes"] = bad

    results_py = (src / "results.py").read_text() if (src / "results.py").is_file() else ""
    env = [
        e
        for e in re.findall(r'os\.environ(?:\.get)?[\[(]"([A-Z_]+)"', app + cli + results_py)
        if STRUCTURAL_VOCABULARY.search(e)
    ]
    if env:
        offenders["environment variables"] = env

    assert not offenders, f"internal vocabulary in public names: {offenders}"


def test_the_readme_tree_matches_the_repository():
    """The README's tree names every tracked file and no untracked one; a recorded file, by its folder.

    The folder of a recorded file is the one directly under its top-level
    folder (validation/<folder>/...).
    """
    readme = (ROOT / "README.md").read_text()
    m = re.search(r"## Files and what they do\n.*?```\n(.*?)\n```", readme, re.S)
    assert m, "no tree of the files"
    tree = m.group(1)
    files = tracked()
    recorded = records() & set(files)
    missing = [f for f in files if f not in recorded and Path(f).name not in tree]
    missing += sorted({Path(f).parts[1] for f in recorded if Path(f).parts[1] not in tree})
    invented = [
        n
        for n in re.findall(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.(?:py|json|csv|toml|yml|md))(?![A-Za-z0-9])", tree)
        if not any(Path(t).name == n for t in tracked())
    ]
    assert not missing, f"tracked files absent from the README tree: {missing}"
    assert not invented, f"README tree names files that are not tracked: {invented}"


def test_the_readme_route_list_is_complete():
    """Every route defined in the package's app.py is documented in the README."""
    app_path = ROOT / "src" / PKG / "app.py"
    if not app_path.is_file():
        pytest.skip("the package has no app.py")
    routes = set(re.findall(r'@app\.(?:get|post)\("([^"]+)"', app_path.read_text()))
    readme = (ROOT / "README.md").read_text()
    documented = set(re.findall(r"(/v1/[a-z_]+|/readyz|/metrics)", readme))
    undocumented = {r for r in routes if r.split("/{")[0] not in documented}
    assert not undocumented, f"routes defined but not documented: {sorted(undocumented)}"


# --- results file --------------------------------------------------------------


# --- program output ------------------------------------------------------------


def test_program_output_is_readable_by_a_stranger():
    """Strings printed by the package's cli.py and by the repository's scripts contain no internal term.

    Printed output is quoted in documentation, so it is held to the same
    vocabulary rules as the documentation.
    """
    jargon = re.compile(
        r"\btrap\b|tripwire|quarantin|vs.published|same.harness|low.FPR.led|open.debt"
        r"|GATE.FAILED|no.GPU|no.network|no.model.download|matched.harness",
        re.I,
    )
    sources = [
        ROOT / "src" / PKG / "cli.py",
        *(ROOT / ".github" / "scripts").glob("*.py"),
    ]
    offenders = {}
    for f in sources:
        if not f.is_file():
            continue
        printed = re.findall(r'print\(\s*\(?\s*((?:f?"[^"]*"\s*)+)', f.read_text())
        bad = sorted({m for chunk in printed for m in jargon.findall(chunk)})
        if bad:
            offenders[str(f.relative_to(ROOT))] = bad
    assert not offenders, f"internal vocabulary in printed output: {offenders}"


# --- README layout -------------------------------------------------------------

PROJECT_MANAGEMENT = re.compile(r"##\s*(?:release.status|work.plan|status)|definition.of.done", re.I)


def test_readme_has_no_project_management_sections():
    """The README has no status or work-plan section and no definition of done; no PLAN.md exists."""
    readme = (ROOT / "README.md").read_text()
    found = sorted(set(PROJECT_MANAGEMENT.findall(readme)))
    assert not found, f"project-management sections in README: {found}"
    assert not (ROOT / "PLAN.md").exists(), "PLAN.md exists at the repository root"


def test_readme_has_the_required_sections():
    """The README has the sections a first-time user needs and a guide to every file.

    Required: a centered title block with badges and a description in italics
    under the title; sections on what the kit needs, a first experiment, what
    it prints, where its results and logs are, how to read a result, testing
    one's own agent, every file and what it does, and the license; and the
    file guide as a tree.
    """
    readme = (ROOT / "README.md").read_text()
    required = [
        "## What you need",
        "## Run your first experiment",
        "## What you see while it runs",
        "## Where your results and logs are",
        "## How to read the result",
        "## Testing your own agent",
        "## Files and what they do",
        "## License",
    ]
    missing = [h for h in required if h not in readme]
    assert not missing, f"README is missing required sections: {missing}"

    assert '<div align="center">' in readme, "no centered title block"
    assert re.search(r"^### \*.+\*$", readme, re.M), "no italic description under the title"
    assert "img.shields.io" in readme, "no badges"
    assert "├──" in readme, "the file guide is not a tree"


def test_no_findings_or_limitations_section():
    """No Markdown file has a heading about findings, limitations, caveats, weaknesses or shortcomings."""
    banned = re.compile(
        r"^##+ .*(limitation|finding|caveat|weakness|shortcoming|what.(?:we|it).(?:get|got).wrong)",
        re.I | re.M,
    )
    hits = {f: banned.findall(body) for f, body in prose_files().items() if f.endswith(".md") and banned.search(body)}
    assert not hits, f"findings or limitations heading in documentation: {hits}"


# --- completeness of this file -------------------------------------------------

EXPECTED_CHECKS = frozenset(
    {
        "test_the_earlier_studies_are_not_tracked",
        "test_no_release_process_or_review_status_language",
        "test_no_venue_attribution_for_this_work",
        "test_no_adversarial_framing",
        "test_no_aphorisms_or_self_praise",
        "test_no_gendered_reference_to_the_author",
        "test_consistent_spelling_convention",
        "test_no_internal_vocabulary_in_filenames",
        "test_no_internal_vocabulary_in_public_names",
        "test_the_readme_tree_matches_the_repository",
        "test_the_readme_route_list_is_complete",
        "test_no_findings_or_limitations_section",
        "test_readme_has_no_project_management_sections",
        "test_readme_has_the_required_sections",
        "test_no_dangling_file_references",
        "test_documented_test_count_matches_reality",
        "test_no_purged_content_survives_in_history",
        "test_commit_messages_are_clean",
        "test_every_tag_points_into_the_current_history",
        "test_no_unsourced_citation",
        "test_license_is_present_and_named",
        "test_ci_blocks_on_secret_and_identity_scans",
        "test_program_output_is_readable_by_a_stranger",
        "test_every_expected_check_is_present",
        "test_no_personal_home_folder_in_history",
    }
)


def test_every_expected_check_is_present():
    """Every name in EXPECTED_CHECKS is defined as a test in this file.

    Removing a check therefore also means removing its name from EXPECTED_CHECKS,
    and the removal shows in the diff.
    """
    defined = set(re.findall(r"^def (test_[a-z_]+)", Path(__file__).read_text(), re.M))
    missing = sorted(EXPECTED_CHECKS - defined)
    assert not missing, f"checks removed without updating EXPECTED_CHECKS: {missing}"


# --- references in documentation ----------------------------------------------


def test_no_dangling_file_references():
    """Every repository path a Markdown file quotes in backticks is tracked, exists, or is ignored by git.

    Only paths under the prefixes in the pattern below are checked.
    """
    files = {k: v for k, v in prose_files().items() if k.endswith(".md")}
    known = set(tracked())
    rx = re.compile(r"`((?:src|tests|calib|release|\.github)/[\w./-]+)`")

    def deliberately_absent(path: str) -> bool:
        """True when git ignores `path`: a directory filled locally is absent from every clone."""
        # git check-ignore matches a directory-only pattern (one ending in "/")
        # against the path with its trailing slash and not without it, so both
        # forms are tried. This runs only for a path absent from the working
        # tree, so an error here shows in a fresh clone and not on a machine
        # where the directory exists.
        for form in (path, path.rstrip("/") + "/"):
            r = subprocess.run(["git", "check-ignore", "-q", form], cwd=ROOT, capture_output=True)
            if r.returncode == 0:
                return True
        return False

    missing = {}
    for f, body in files.items():
        gone = [
            m for m in rx.findall(body) if m not in known and not (ROOT / m).exists() and not deliberately_absent(m)
        ]
        if gone:
            missing[f] = sorted(set(gone))
    assert not missing, f"documentation references files that do not exist: {missing}"


def test_documented_test_count_matches_reality():
    """Every test count stated in README.md equals the number pytest collects."""
    r = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "--collect-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    m = re.search(r"(\d+) tests? collected", r.stdout)
    if not m:
        pytest.skip("pytest --collect-only reported no test count")
    actual = int(m.group(1))
    for name in ("README.md",):
        for claimed in re.findall(r"(\d+)\s+(?:offline|tests)", (ROOT / name).read_text()):
            assert int(claimed) == actual, f"{name} states {claimed} tests; pytest collects {actual}"


# --- history and commit messages -----------------------------------------------

HISTORY_PATTERNS = (
    pytest.param(
        r"anonym|double.blind|under.review|flip.public",
        "publication-status language",
        True,
        id="publication-status",
    ),
    pytest.param(r"/User[s]/", "absolute home-directory path", False, id="home-directory-path"),
)


def _reachable_hits(pattern: str, skip_records: bool = False) -> list[str]:
    """`commit:path` for each file in a reachable commit that matches `pattern`.

    LICENSE, the CI workflow and this file are skipped, and with skip_records
    the recorded files are too, by their path or by their content.
    """
    commits = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    hits: list[str] = []
    for c in commits:
        r = subprocess.run(
            ["git", "grep", "-l", "-iE", pattern, c, "--", ".", ":!LICENSE", f":!{CI}"]
            + [f":!{s}" for s in (SELF, *SELF_EARLIER_PATHS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        hits += [ln for ln in r.stdout.splitlines() if ln.strip()]
    if skip_records:
        recorded = records()
        recorded_sha = {manifest()[p]["sha256"] for p in recorded}
        hits = [h for h in hits if h.split(":", 1)[1] not in recorded and _blob_sha256(h) not in recorded_sha]
    return hits


def _blob_sha256(commit_path: str) -> str:
    """SHA-256 of a file as a commit holds it, so a recorded file is known by its content under any path."""
    blob = subprocess.run(["git", "cat-file", "blob", commit_path], cwd=ROOT, capture_output=True).stdout
    return hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize("pattern,label,skip_records", HISTORY_PATTERNS)
def test_no_purged_content_survives_in_history(pattern, label, skip_records):
    """No file in any commit reachable from a branch or tag matches the pattern.

    Every reachable commit is searched, not only the working tree, so content
    that a later commit removed is still found in the commits that hold it.
    """
    hits = _reachable_hits(pattern, skip_records)
    assert not hits, f"{label} in reachable history: {sorted(set(hits))[:5]}"


def test_no_personal_home_folder_in_history():
    """Every Linux home folder a file in a reachable commit names is one of ACCOUNTS."""
    commits = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    found: set[str] = set()
    for c in commits:
        r = subprocess.run(
            ["git", "grep", "-a", "-h", "-o", "-E", "/hom[e]/[A-Za-z0-9_.-]+/", c, "--", ".", ":!LICENSE"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            errors="replace",
        )
        found |= {ln.split("/")[2] for ln in r.stdout.splitlines() if ln.count("/") >= 3}
    assert found <= ACCOUNTS, f"home folders that are not test accounts: {sorted(found - ACCOUNTS)}"


@pytest.mark.parametrize("pattern,label,skip_records", HISTORY_PATTERNS)
def test_commit_messages_are_clean(pattern, label, skip_records):
    """No commit message on any branch or tag matches the pattern."""
    msgs = subprocess.run(["git", "log", "--all", "--format=%B"], cwd=ROOT, capture_output=True, text=True).stdout
    found = re.findall(pattern, msgs, re.I)
    assert not found, f"{label} in commit messages: {sorted(set(found))}"


def test_every_tag_points_into_the_current_history():
    """Every tag points at a commit reachable from HEAD."""
    tags = subprocess.run(["git", "tag", "--list"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not tags:
        pytest.skip("no tags in this checkout")
    head = set(subprocess.run(["git", "rev-list", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.split())
    for tag in tags:
        sha = subprocess.run(
            ["git", "rev-list", "-n", "1", tag], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
        assert sha in head, f"tag {tag} points at {sha[:8]}, which is not reachable from HEAD"


# --- citations -----------------------------------------------------------------


def test_no_unsourced_citation():
    """A citation by author list, arXiv identifier or parenthesized year needs a record outside documentation.

    If a Markdown file carries a citation in one of these forms, at least one
    tracked file that is not Markdown must carry
    one too. A work cited by its title alone needs no record.
    """
    claims = re.compile(r"\bet al\.|arXiv:\s*\d|\b(?:19|20)\d{2}[a-z]?\)")
    files = prose_files()
    # A citation counts as supported only when a file other than documentation carries one.
    supported = any(claims.search(body) for g, body in files.items() if not g.endswith(".md"))
    offenders = {}
    for f, body in files.items():
        if not f.endswith(".md"):
            continue
        found = claims.findall(body)
        if found and not supported:
            offenders[f] = sorted(set(found))
    assert not offenders, (
        f"citations with no record outside documentation: {offenders}; cite the work by its "
        "title, or ship the record that carries the identifier"
    )


# --- license and CI ------------------------------------------------------------


def test_license_is_present_and_named():
    """LICENSE holds the Apache License or the MIT License."""
    s = (ROOT / "LICENSE").read_text()
    assert "Apache License" in s or "MIT License" in s, "LICENSE is neither the Apache License nor the MIT License"


def test_ci_blocks_on_secret_and_identity_scans():
    """CI runs a secret scan and an identity scan, and the test job waits for both.

    The identity scan reads the names it refuses from the IDENTITY_PATTERN
    repository secret, so the workflow itself holds no names pattern.
    """
    ci = (ROOT / CI).read_text()
    assert "gitleaks" in ci, "no secret scan"
    assert re.search(r"^\s{2}identity:", ci, re.M), "no identity scan job"
    assert re.search(r"needs:\s*\[secrets,\s*identity\]", ci), "the test job does not wait for both scans"
    assert "secrets.IDENTITY_PATTERN" in ci, "the identity pattern is not read from a repository secret"
    assert not re.search(r"git grep[^\n]*\|[a-z]+\|", ci), "a literal names pattern is in the workflow"
