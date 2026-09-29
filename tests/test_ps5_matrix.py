"""The PolyScope 5 version matrix's pure parts (urcap/ps5_matrix.py): the port table, the
compose file held to it, the Docker Hub tag checker, and the polyscope.log matcher."""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "urcap"))

import ps5_matrix as m  # noqa: E402

COMPOSE = (REPO / "docker-compose.ps5-matrix.yml").read_text(encoding="utf-8")
DEFAULT_COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
WORKFLOW = (REPO / ".github" / "workflows" / "urcap5-matrix.yml").read_text(encoding="utf-8")
FIXTURES = REPO / "tests" / "fixtures" / "ps5_matrix"


# -- ports ------------------------------------------------------------------------------------


def test_matrix_host_ports_never_collide_with_each_other_or_the_default_sims():
    seen: dict[int, str] = {}
    for v in m.MATRIX.values():
        for port in m.host_ports(v):
            assert port not in seen, f"{v.tag} reuses host port {port} of {seen.get(port)}"
            assert port not in m.DEFAULT_PORTS, f"{v.tag} host port {port} is a default-sim/cockpit port"
            seen[port] = v.tag


def test_default_ports_cover_every_host_port_docker_compose_yml_publishes():
    published = {int(a) for a, _ in re.findall(r'"(\d+):(\d+)"', DEFAULT_COMPOSE)}
    assert published, "docker-compose.yml publishes nothing? the regex drifted"
    assert published <= m.DEFAULT_PORTS, f"add {sorted(published - m.DEFAULT_PORTS)} to DEFAULT_PORTS"


def _service_block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\n)+)", COMPOSE, re.M)
    assert match, f"{name} is not a service in docker-compose.ps5-matrix.yml"
    return match.group(1)


@pytest.mark.parametrize("minor", list(m.MATRIX))
def test_compose_file_publishes_exactly_the_table(minor):
    v = m.MATRIX[minor]
    block = _service_block(v.service)
    assert f"image: {m.IMAGE}:{v.tag}\n" in block
    mapped = dict((int(c), int(h)) for h, c in re.findall(r'"(\d+):(\d+)"', block))
    assert mapped == {29999: v.dashboard, 30001: v.primary, 30004: v.rtde, 6080: v.novnc}


def test_compose_file_has_no_service_the_matrix_does_not_name():
    services = set(re.findall(r"^  (ursim-[\w-]+):$", COMPOSE, re.M))
    assert services == {v.service for v in m.MATRIX.values()}


def test_compose_file_header_table_matches_the_matrix():
    for v in m.MATRIX.values():
        cells = [v.service, re.escape(v.tag), v.dashboard, v.primary, v.rtde, v.novnc, v.pick]
        row = r"#\s+" + r"\s+".join(map(str, cells))
        assert re.search(row, COMPOSE), f"header row for {v.service} is stale"


def test_workflow_matrix_is_the_matrix():
    listed = re.search(r"polyscope: \[(.*)\]", WORKFLOW)
    assert listed
    assert [s.strip().strip('"') for s in listed.group(1).split(",")] == list(m.MATRIX)


def test_matrix_is_the_newest_patch_of_each_minor_oldest_first():
    tags = [v.tag for v in m.MATRIX.values()]
    assert tags == sorted(tags, key=m.parse_tag)
    assert len({v.minor for v in m.MATRIX.values()}) == m.MATRIX_SIZE == len(tags)


# -- Docker Hub tags --------------------------------------------------------------------------

# The listing on 2026-09-28 (hub.docker.com, universalrobots/ursim_e-series, 51 tags).
HUB_2026_09_28 = (
    "latest 5.26 5.25 5.24 5.23 5.22 5.21 5.20 5.19 5.18 5.17 5.16 5.15 5.14 5.13 5.12 5.11 "
    "5.10 5.9 5.26.1 5.26.0 5.25.2 5.25.1 5.25.0 5.24.0 5.23.0 5.22.2 5.22.0 5.21.3 5.21.0 "
    "5.20.0 5.19.0 5.18.1 5.18.0 5.17.3 5.16.1 5.15.2 5.14.6 5.13.1 5.12.8 5.12.7 5.12.6 "
    "5.12.5 5.11.11 5.10.2 5.9.4 5.8 5.7 5.6 5.5 5.4"
).split()


@pytest.mark.parametrize(
    "tag, parsed",
    [
        ("5.26.1", (5, 26, 1)),
        ("5.9.4", (5, 9, 4)),
        ("5.26", None),
        ("latest", None),
        ("5.26.1-rc1", None),
        ("6.0.0", None),
        ("05.26.1", None),
        ("5.026.1", None),
        (" 5.26.1", None),
        ("5.26.1\n", None),
        ("", None),
        ("٥.٢٦.١", None),  # Arabic-Indic digits: \d matches them, int() would accept them
    ],
)
def test_parse_tag(tag, parsed):
    assert m.parse_tag(tag) == parsed


def test_the_real_listing_gives_the_committed_matrix():
    assert m.newest_minors(HUB_2026_09_28) == [v.tag for v in m.MATRIX.values()]
    assert m.tag_drift(HUB_2026_09_28, [v.tag for v in m.MATRIX.values()]) == []


def test_a_new_minor_says_add_it_and_drop_the_oldest():
    tags = [*HUB_2026_09_28, "5.27.0"]
    assert m.tag_drift(tags, ["5.24.0", "5.25.2", "5.26.1"]) == [
        "PolyScope 5.27 exists: add it and drop 5.24"
    ]


def test_a_new_patch_says_bump_it():
    tags = [*HUB_2026_09_28, "5.25.10"]
    assert m.tag_drift(tags, ["5.24.0", "5.25.2", "5.26.1"]) == [
        "PolyScope 5.25.10 exists: bump 5.25.2 to it"
    ]


def test_a_matrix_tag_gone_from_the_hub_is_reported():
    tags = [t for t in HUB_2026_09_28 if t != "5.25.2"]
    problems = m.tag_drift(tags, ["5.24.0", "5.25.2", "5.26.1"])
    assert f"{m.IMAGE}:5.25.2 is not on Docker Hub" in problems


def test_non_polyscope5_tags_never_count():
    tags = [*HUB_2026_09_28, "6.0.0", "5.99", "5.30.0-beta", "latest"]
    assert m.tag_drift(tags, ["5.24.0", "5.25.2", "5.26.1"]) == []


@pytest.mark.parametrize("seed", range(200))
def test_newest_minors_is_order_independent_and_picks_each_minors_max_patch(seed):
    rng = random.Random(seed)
    versions = {(5, rng.randint(0, 40), rng.randint(0, 30)) for _ in range(rng.randint(0, 40))}
    junk = ["latest", "5.1", "6.1.0", "5.2.3-rc", "x"]
    tags = [".".join(map(str, v)) for v in versions] + rng.sample(junk, rng.randint(0, len(junk)))
    rng.shuffle(tags)
    n = rng.randint(1, 5)
    got = m.newest_minors(tags, n)
    # Reference: group by minor, max patch, newest n minors, oldest first.
    by_minor: dict[int, int] = {}
    for _, minor, patch in versions:
        by_minor[minor] = max(patch, by_minor.get(minor, -1))
    want = [f"5.{mi}.{by_minor[mi]}" for mi in sorted(by_minor)[-n:]]
    assert got == want
    assert m.newest_minors(list(reversed(tags)), n) == got
    # A matrix equal to the answer never drifts.
    assert m.tag_drift(tags, got) == []


def test_the_patch_comparison_is_numeric_not_lexical():
    assert m.newest_minors(["5.25.9", "5.25.10", "5.25.2"], 1) == ["5.25.10"]
    assert m.newest_minors(["5.9.4", "5.10.2", "5.100.0"], 2) == ["5.10.2", "5.100.0"]


# -- the URCap's evidence: polyscope.log errors, Felix shell ps / services ----------------


def test_log_errors_ignore_a_log_that_never_names_the_urcap():
    log = "INFO Starting PolyScope\njava.lang.NullPointerException\n\tat com.ur.foo.Bar(Bar.java:1)\n"
    assert m.log_errors(log) == []


def test_log_errors_on_a_real_clean_load_are_empty():
    """tests/fixtures/ps5_matrix/polyscope-5.24.0-clean.log is a real excerpt (CI matrix
    run 36510721492, 2026-09-28): our jar added and located by URCapHelper, next to UR's
    own ERROR lines and a licence-omitted URCap that is not ours."""
    log = (FIXTURES / "polyscope-5.24.0-clean.log").read_text(encoding="utf-8")
    assert "realsense-pilot-ps5.jar" in log and "ERROR" in log and "Omitted licenced URCap" in log
    assert m.log_errors(log) == []


def test_log_errors_flag_a_stack_frame_in_our_package_with_its_exception():
    # The shape of the one PolyScope-5-only bug so far (README: setBorder refused).
    log = (
        "12:00:01 INFO something\n"
        "com.ur.urcap.api.domain.AuthorizationException: Method not supported from URCaps\n"
        "\tat com.ur.polyscope.Guard.check(Guard.java:10)\n"
        "\tat com.olympuscontrols.realsensepilot.PilotView.buildUI(PilotView.java:42)\n"
    )
    errors = m.log_errors(log)
    assert errors[0].startswith("com.ur.urcap.api.domain.AuthorizationException")
    assert any("PilotView.buildUI" in e for e in errors)


@pytest.mark.parametrize(
    "line",
    [
        "ERROR Could not start bundle com.olympuscontrols.realsensepilot [42]: Unresolved constraint",
        "INFO Omitted licenced URCap: com.olympuscontrols.realsensepilot due to missing license.",
        "WARN URCap realsense-pilot-ps5.jar rejected: incompatible API version",
    ],
)
def test_log_errors_flag_a_failure_line_naming_the_urcap(line):
    assert m.log_errors(f"INFO fine\n{line}\nINFO fine\n") == [line]


def test_log_errors_ignore_other_bundles_that_merely_look_similar():
    log = "ERROR bundle com.example.realsense_other failed\nERROR com.olympus.other failed\n"
    assert m.log_errors(log) == []


PS = """
START LEVEL 1
   ID   State         Level  Name
[   0] [Active     ] [    0] System Bundle (7.0.3)
[   1] [Active     ] [    1] aopalliance (1.0)
[ 164] [Active     ] [    1] RealSense Pilot (0.4.0)
[ 165] [Resolved   ] [    1] Other Thing (1.0)
-> """


def test_parse_ps_reads_every_row():
    rows = m.parse_ps(PS)
    assert [r["id"] for r in rows] == [0, 1, 164, 165]
    assert rows[2] == {"id": 164, "state": "Active", "level": 1, "name": "RealSense Pilot (0.4.0)"}


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("felix-ps-*.txt")))
def test_the_urcap_is_active_in_real_shell_output(fixture):
    """A real ``ps`` from each version's Felix remote shell (CI matrix run 36510721492,
    2026-09-28, URCap 0.4.0): every row parses and ours is Active."""
    text = fixture.read_text(encoding="utf-8")
    rows = m.parse_ps(text)
    assert rows[0] == {"id": 0, "state": "Active", "level": 0, "name": "System Bundle (7.0.3)"}
    assert len(rows) == sum(1 for line in text.splitlines() if line.startswith("["))
    ours = m.find_bundle(rows, "RealSense Pilot", "0.4.0")
    assert ours and ours["state"] == "Active"


@pytest.mark.parametrize(
    "name, version, found",
    [
        ("RealSense Pilot", "0.4.0", 164),
        ("RealSense Pilot", "0.5.0", None),  # a stale jar in the image is not the one under test
        ("RealSense", "0.4.0", None),
        ("realsense pilot", "0.4.0", None),
    ],
)
def test_find_bundle_matches_name_and_version_exactly(name, version, found):
    row = m.find_bundle(m.parse_ps(PS), name, version)
    assert (row["id"] if row else None) == found


def test_missing_services_names_what_the_activator_did_not_register():
    both = (
        "RealSense Pilot (164) provides:\n-------------------------------\n"
        "objectClass = com.ur.urcap.api.contribution.installation.swing.SwingInstallationNodeService\n"
        "service.id = 301\n----\n"
        "objectClass = [com.ur.urcap.api.contribution.program.swing.SwingProgramNodeService]\n"
        "service.id = 302\n"
    )
    assert m.missing_services(both) == []
    one = both.split("service.id = 301")[0]
    assert m.missing_services(one) == ["com.ur.urcap.api.contribution.program.swing.SwingProgramNodeService"]
    assert m.missing_services("") == list(m.NODE_SERVICES)
    # Named in prose, not as an objectClass: not registered.
    assert m.missing_services("could not register SwingProgramNodeService") == list(m.NODE_SERVICES)


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("felix-services-*.txt")))
def test_missing_services_on_real_listings(fixture):
    """Real ``services <id>`` listings of the URCap from the CI matrix (one per version)."""
    assert m.missing_services(fixture.read_text(encoding="utf-8")) == [], fixture.name
