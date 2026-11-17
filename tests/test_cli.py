from __future__ import annotations

import json
from pathlib import Path

import pytest

from cachebreak.cli import main, parse_since

from factory import Transcript


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    good = Transcript(session_id="aaaa1111-0000-0000-0000-000000000000")
    good.user("a").request(write=20_000, read=0).user("b").request(write=500, read=20_100)
    good.write(root, "-work-good")
    bad = Transcript(session_id="bbbb2222-0000-0000-0000-000000000000", cwd="/work/bad")
    bad.user("a").request(write=30_000, read=0)
    bad.user("b").request(write=30_000, read=0, model="claude-opus-5-5")
    bad.compact().request(write=5_000, read=0, model="claude-opus-5-5")
    bad.write(root, "-work-bad")
    return root


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_default_command_lists_sessions(tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    code, out = run(capsys, "--root", str(root), "--no-color")
    assert code == 0
    assert "aaaa1111" in out and "bbbb2222" in out
    code, out = run(capsys, "--root", str(root), "-n", "1", "--no-color")  # list-only flag
    assert code == 0 and len(out.strip().splitlines()) == 2
    code, out = run(capsys, "list", "--root", str(root), "--project", "good", "--json")
    data = json.loads(out)
    assert [d["session_id"][:8] for d in data] == ["aaaa1111"]


def test_show_by_prefix_text_and_json(tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    code, out = run(capsys, "show", "bbbb", "--root", str(root), "--no-color")
    assert code == 0
    assert "Cache breaks: 2" in out
    assert "model-switch" in out and "[proven]" in out
    code, out = run(capsys, "show", "bbbb", "--root", str(root), "--json")
    data = json.loads(out)
    assert [b["cause"] for b in data["breaks"]] == ["model-switch", "compaction"]
    assert data["evidence"] == "request records"


def test_show_unknown_session_exits_2(tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    assert main(["show", "zzzz", "--root", str(root)]) == 2
    assert "no session matching" in capsys.readouterr().err


def test_check_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    code, out = run(capsys, "check", "aaaa", "--root", str(root))
    assert code == 0 and out.startswith("ok:")
    code, out = run(capsys, "check", "aaaa", "bbbb", "--root", str(root))
    assert code == 1
    assert "FAIL bbbb2222" in out and "model-switch" in out
    assert "compaction" not in out  # allowed by default
    code, out = run(capsys, "check", "bbbb", "--root", str(root),
                    "--allow", "compaction,model-switch")
    assert code == 0
    code, out = run(capsys, "check", "bbbb", "--root", str(root), "--max-usd", "1e9", "--json")
    assert code == 0 and json.loads(out)["ok"] is True
    assert main(["check", "bbbb", "--root", str(root), "--allow", "bogus"]) == 2


def test_check_accepts_a_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    path = next((root / "-work-bad").glob("*.jsonl"))
    assert main(["check", str(path)]) == 1


def test_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    code, out = run(capsys, "summary", "--root", str(root), "--since", "all", "--no-color")
    assert code == 0
    assert "2 sessions" in out and "model-switch" in out and "compaction" in out
    code, out = run(capsys, "summary", "--root", str(root), "--since", "all", "--json")
    data = json.loads(out)
    assert data["causes"]["model-switch"]["breaks"] == 1
    # The synthetic sessions are dated 2026-11-17; a 1-minute window from "now" may exclude them.
    code, out = run(capsys, "summary", "--root", str(root), "--since", "2000-01-01", "--json")
    assert json.loads(out)["sessions"] == 2


def test_parse_since() -> None:
    assert parse_since(None) is None
    assert parse_since("7d") is not None
    assert parse_since("2026-11-01").tzinfo is not None  # type: ignore[union-attr]
    with pytest.raises(Exception):
        parse_since("yesterday")


def test_missing_root_is_graceful(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(capsys, "list", "--root", str(tmp_path / "nope"))
    assert code == 0 and "no sessions" in out
    assert main(["show", "--root", str(tmp_path / "nope")]) == 2


def test_claude_config_dir_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    make_root(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    code, out = run(capsys, "show", "aaaa", "--json")
    assert code == 0 and json.loads(out)["breaks"] == []


def test_bad_prices_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path)
    assert main(["show", "aaaa", "--root", str(root), "--prices", str(tmp_path / "x.json")]) == 2
    assert "cachebreak:" in capsys.readouterr().err
