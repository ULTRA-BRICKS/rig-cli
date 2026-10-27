"""socket_owner_pids must read the unix-socket name field of Linux and macOS lsof.

Linux lsof prints the NAME of a unix socket as ``/path/sock type=STREAM``; macOS prints the
bare path. The owner scan is the only guard that stops the watchdog from starting a second
tmux server over a live one whose socket file was unlinked, so a name it cannot match fails
open. These tests drive the parser with fake lsof output (no tmux, no lsof binary needed).
"""

from types import SimpleNamespace

import pytest

from riglib import tmux_watchdog as wd


def fake_lsof(monkeypatch, stdout, *, returncode=0, stderr=""):
    monkeypatch.setattr(
        wd.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        ),
    )


def test_linux_type_suffix_is_not_part_of_the_socket_path(monkeypatch, tmp_path):
    sock = tmp_path / "t.sock"
    fake_lsof(monkeypatch, f"p123\nn{sock} type=STREAM\n")
    assert wd.socket_owner_pids(sock) == {123}


@pytest.mark.parametrize("kind", ["STREAM", "DGRAM", "SEQPACKET"])
def test_every_linux_socket_type_suffix_is_recognised(monkeypatch, tmp_path, kind):
    sock = tmp_path / "t.sock"
    fake_lsof(monkeypatch, f"p7\nn{sock} type={kind}\n")
    assert wd.socket_owner_pids(sock) == {7}


@pytest.mark.parametrize("state", ["LISTEN", "CONNECTED", "UNCONNECTED"])
def test_a_state_trailer_after_the_type_is_stripped_too(monkeypatch, tmp_path, state):
    # lsof's table output prints "type=STREAM (LISTEN)"; -F does not, but a build that did
    # must still be matched rather than fail open.
    sock = tmp_path / "t.sock"
    fake_lsof(monkeypatch, f"p8\nn{sock} type=STREAM ({state})\n")
    assert wd.socket_owner_pids(sock) == {8}


def test_plain_macos_name_without_suffix_still_matches(monkeypatch, tmp_path):
    sock = tmp_path / "t.sock"
    fake_lsof(monkeypatch, f"p123\nn{sock}\n")
    assert wd.socket_owner_pids(sock) == {123}


def test_path_with_spaces_is_kept_whole(monkeypatch, tmp_path):
    sock = tmp_path / "dir with spaces" / "t.sock"
    fake_lsof(monkeypatch, f"p5\nn{sock} type=STREAM\n")
    assert wd.socket_owner_pids(sock) == {5}
    # a space-bearing prefix of the real path is a different socket
    fake_lsof(monkeypatch, f"p5\nn{tmp_path / 'dir'} type=STREAM\n")
    assert wd.socket_owner_pids(sock) == set()


def test_type_lookalike_inside_the_path_is_not_stripped(monkeypatch, tmp_path):
    sock = tmp_path / "a type=STREAM" / "t.sock"
    fake_lsof(monkeypatch, f"p9\nn{sock} type=STREAM\n")
    assert wd.socket_owner_pids(sock) == {9}
    # the sibling the lookalike would be truncated to is a different socket
    fake_lsof(monkeypatch, f"p9\nn{tmp_path / 'a'} type=STREAM\n")
    assert wd.socket_owner_pids(sock) == set()


def test_socket_named_like_the_suffix_still_blocks(monkeypatch, tmp_path):
    # macOS prints the bare path, so a socket literally named "s type=STREAM" has no
    # suffix to strip. Both readings of the field are tried; an ambiguous name must block.
    sock = tmp_path / "s type=STREAM"
    fake_lsof(monkeypatch, f"p4\nn{sock}\n")
    assert wd.socket_owner_pids(sock) == {4}
    fake_lsof(monkeypatch, f"p4\nn{sock} type=STREAM\n")
    assert wd.socket_owner_pids(sock) == {4}


def test_a_different_socket_is_not_an_owner(monkeypatch, tmp_path):
    other = tmp_path / "other.sock"
    fake_lsof(monkeypatch, f"p123\nn{other} type=STREAM\np124\nn{other}\n")
    assert wd.socket_owner_pids(tmp_path / "t.sock") == set()


def test_only_the_matching_descriptors_owners_are_returned(monkeypatch, tmp_path):
    sock = tmp_path / "t.sock"
    out = (
        f"p10\nn{sock} type=STREAM\nn->0xdeadbeef\n"
        f"p11\nn{tmp_path / 'x.sock'} type=STREAM\n"
        f"p12\nn{sock} type=STREAM\n"
    )
    fake_lsof(monkeypatch, out)
    assert wd.socket_owner_pids(sock) == {10, 12}


@pytest.mark.parametrize(
    "returncode,stderr",
    [(0, "lsof: WARNING: partial results"), (1, "lsof: WARNING"), (2, "")],
)
def test_an_incomplete_scan_still_fails_closed(
    monkeypatch, tmp_path, returncode, stderr
):
    sock = tmp_path / "t.sock"
    # even when the output already names the owner, a failed scan is not an answer
    fake_lsof(
        monkeypatch,
        f"p123\nn{sock} type=STREAM\n",
        returncode=returncode,
        stderr=stderr,
    )
    with pytest.raises(wd.InspectionUnavailable):
        wd.socket_owner_pids(sock)
