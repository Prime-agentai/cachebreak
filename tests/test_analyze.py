from __future__ import annotations

from pathlib import Path

import pytest

from cachebreak import analyze, diff_shapes, load_session
from cachebreak.analyze import (COMPACTION, HISTORY, MODEL, PARAMS, SHAPE, TTL_EXPIRED,
                                UNEXPLAINED, VERSION)
from cachebreak.pricing import TABLE
from cachebreak.transcript import Usage

from factory import BASE_SHAPE, Transcript


def steady(t: Transcript, n: int = 3, start: int = 20_000, step: int = 2_000) -> Transcript:
    """n requests whose prefix grows and is always read back."""
    ctx = start
    t.user("hello").request(write=ctx, read=0)
    for _ in range(n - 1):
        t.user("more").request(write=step, read=ctx + 100)
        ctx += step + 100
    return t


def report(t: Transcript, tmp_path: Path, **kw: object):  # type: ignore[no-untyped-def]
    return analyze(load_session(t.write(tmp_path)), **kw)  # type: ignore[arg-type]


def test_usage_split_and_totals() -> None:
    u = Usage.from_api({"input_tokens": 3, "cache_creation_input_tokens": 100,
                        "cache_read_input_tokens": 50, "output_tokens": 7,
                        "cache_creation": {"ephemeral_1h_input_tokens": 60,
                                           "ephemeral_5m_input_tokens": 40}})
    assert (u.write_1h, u.write_5m, u.prompt) == (60, 40, 153)
    # Older transcripts have no split: treat writes as 5m.
    old = Usage.from_api({"cache_creation_input_tokens": 10})
    assert (old.write_5m, old.write_1h) == (10, 0)
    assert Usage.from_api(None) == Usage()


def test_duplicate_assistant_entries_count_once(tmp_path: Path) -> None:
    t = Transcript().user("hi").request(write=5000, read=0, blocks=4)
    r = report(t, tmp_path)
    assert r.requests == 1
    assert r.totals.write == 5000


def test_steady_session_has_no_breaks(tmp_path: Path) -> None:
    r = report(steady(Transcript(), 5), tmp_path)
    assert r.breaks == []
    assert r.requests == 5
    assert r.hit_rate > 0.5


def test_model_switch_is_proven(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=30_000, read=0)
    t.user("b").request(write=30_500, read=0, model="claude-opus-5-5")
    r = report(t, tmp_path)
    (b,) = r.breaks
    assert b.cause == MODEL and b.proven
    assert "claude-sonnet-5-5 -> claude-opus-5-5" in b.evidence[0].detail
    assert b.lost_tokens == 30_000 + 3
    opus = TABLE["opus-5.5"]  # the request that paid the re-write
    assert b.cost_usd == pytest.approx(b.lost_tokens * (opus.write_1h - opus.read) / 1e6)


def test_ttl_expiry_5m(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=40_000, read=0, ttl="5m")
    t.wait(minutes=7).user("b").request(write=40_200, read=0, ttl="5m")
    (b,) = report(t, tmp_path).breaks
    assert b.cause == TTL_EXPIRED and not b.proven
    assert "idle 7m02s > 5m00s TTL" in b.evidence[0].detail
    son = TABLE["sonnet-5.5"]
    assert b.cost_usd == pytest.approx(b.lost_tokens * (son.write_5m - son.read) / 1e6)


def test_gap_within_1h_ttl_is_not_ttl(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=40_000, read=0, ttl="1h")
    t.wait(minutes=30).user("b").request(write=40_000, read=0, ttl="1h")
    (b,) = report(t, tmp_path).breaks
    assert b.cause == UNEXPLAINED
    assert b.evidence[0].detail == "same model, shape and history"


def test_tools_change_is_proven_with_names(tmp_path: Path) -> None:
    new_shape = {**BASE_SHAPE, "tools": BASE_SHAPE["tools"] + [
        {"name": "mcp__github__create_issue", "description": "x", "input_schema": {}}]}
    t = Transcript().user("a").request(write=30_000, read=0)
    t.user("b").request(write=29_000, read=1_500, shape=new_shape)
    (b,) = report(t, tmp_path).breaks
    assert b.cause == SHAPE and b.proven
    assert "tools added: mcp__github__create_issue" in b.evidence[0].detail
    assert b.kept_tokens == 1_500


def test_system_prompt_change_points_at_the_char(tmp_path: Path) -> None:
    new_shape = {**BASE_SHAPE, "system": [BASE_SHAPE["system"][0],
                                          {"type": "text", "text": "Be careless with files."}]}
    t = Transcript().user("a").request(write=30_000, read=0)
    t.user("b").request(write=29_000, read=1_000, shape=new_shape)
    (b,) = report(t, tmp_path).breaks
    assert b.cause == SHAPE
    assert "system[1] changed at char 7" in b.evidence[0].detail


def test_diff_shapes_variants() -> None:
    a = {"system": [{"text": "x"}], "tools": [{"name": "A"}, {"name": "B"}]}
    assert diff_shapes(a, {"system": [{"text": "x"}], "tools": [{"name": "B"}, {"name": "A"}]}) \
        == ["tool order changed"]
    assert diff_shapes(a, {"system": [], "tools": [{"name": "A"}]}) == \
        ["system[0] removed", "tools removed: B"]
    assert diff_shapes(a, {**a, "tools": [{"name": "A", "d": 1}, {"name": "B"}]}) == \
        ["tool definitions changed: A"]
    assert diff_shapes(a, a) == []


def test_history_rewrite_is_proven(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=30_000, read=0)
    t.user("b").request(write=2_000, read=30_100)
    t.user("c").request(write=33_000, read=900, rewrite_from=1)
    (b,) = report(t, tmp_path).breaks
    assert b.cause == HISTORY and b.proven
    assert b.evidence[0].detail.startswith("message #1")


def test_compaction_wins_and_is_by_design(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=150_000, read=0)
    t.compact().user("summary", isCompactSummary=True)
    t.wait(hours=2).request(write=40_000, read=0)  # also idle past the TTL: compaction wins
    r = report(t, tmp_path)
    # Context shrank: only the re-written part (40k) counts, not the 150k dropped.
    (b,) = r.breaks
    assert b.cause == COMPACTION and b.proven
    assert [e.cause for e in b.evidence] == [COMPACTION, TTL_EXPIRED]
    assert b.lost_tokens == 40_000


def test_tiny_post_compaction_prefix_is_not_a_break(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=150_000, read=0)
    t.compact().user("summary", isCompactSummary=True).request(write=8_000, read=0)
    assert report(t, tmp_path).breaks == []


def test_version_change_is_inferred(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=30_000, read=0, records=False)
    t.user("b").request(write=30_000, read=0, records=False, version="2.1.400")
    (b,) = report(t, tmp_path).breaks
    assert b.cause == VERSION and not b.proven


def test_usage_only_transcript_says_so(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=30_000, read=0, records=False)
    t.wait(minutes=10).user("b").request(write=30_000, read=0, records=False, ttl="1h")
    r = report(t, tmp_path)
    assert r.evidence_level == "usage only"
    (b,) = r.breaks
    assert b.cause == UNEXPLAINED
    assert "no request records" in b.evidence[0].detail


def test_small_rewrites_are_ignored(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=30_000, read=0)
    t.user("b").request(write=900, read=29_200)  # lost 803 < 1024
    assert report(t, tmp_path).breaks == []
    assert len(report(t, tmp_path, min_tokens=500, min_ratio=0.0).breaks) == 1


def test_sidechain_is_its_own_chain(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=80_000, read=0)
    t.request(write=6_000, read=0, sidechain=True, agent="explore1")  # subagent's own prefix
    t.request(write=1_000, read=6_000, sidechain=True, agent="explore1")
    t.user("b").request(write=2_000, read=80_100)
    r = report(t, tmp_path)
    assert r.breaks == []
    assert r.chains == {"main": 2, "agent:explore1": 2}


def test_subagent_files_load_as_chains(tmp_path: Path) -> None:
    main = Transcript().user("a").request(write=50_000, read=0)
    path = main.write(tmp_path)
    sub = Transcript()
    sub.n = 100  # request ids are globally unique in real transcripts
    sub.user("task").request(write=7_000, read=0)
    sub.request(write=500, read=7_100)
    sub_dir = path.with_suffix("") / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-a1b2.jsonl").write_text("\n".join(sub.lines) + "\n")
    r = analyze(load_session(path))
    assert r.chains == {"main": 1, "agent:a1b2": 2}
    assert r.totals.write == 57_500


def test_malformed_and_truncated_lines_are_counted(tmp_path: Path) -> None:
    t = steady(Transcript(), 2).raw("{not json").raw('["a list"]')
    t.raw('{"type":"assistant","message":{"usage":{"input_tok')
    r = report(t, tmp_path)
    assert r.session.bad_lines == 3
    assert r.requests == 2


def test_request_ref_before_record_is_linked(tmp_path: Path) -> None:
    # Some writers flush the assistant entry before the api-request record.
    t = Transcript().user("a").request(write=30_000, read=0)
    lines = t.lines
    req_idx = next(i for i, ln in enumerate(lines) if '"type": "api-request"' in ln)
    moved = lines.pop(req_idx)
    lines.append(moved)
    s = load_session(t.write(tmp_path))
    assert s.requests[0].record is not None
    assert s.has_request_records


def test_costliest_context_ranks_big_tool_results(tmp_path: Path) -> None:
    t = Transcript().user("a").request(
        write=20_000, read=0, tool_use=("t1", "Read", {"file_path": "/repo/package-lock.json"}))
    t.tool_result("t1", "x" * 200_000)
    t.request(write=50_100, read=20_000, tool_use=("t2", "Bash", {"command": "ls"}))
    t.tool_result("t2", "a\nb\n")
    t.request(write=50, read=70_100)
    t.user("next").request(write=60, read=70_150)
    r = report(t, tmp_path)
    top = r.taxes[0]
    assert top.label == "Read /repo/package-lock.json"
    assert top.tool == "Read"
    assert top.tokens == pytest.approx(50_000, rel=0.01)  # growth minus previous output
    assert top.carried == 2
    son = TABLE["sonnet-5.5"]
    assert top.cost_usd == pytest.approx(top.tokens * (son.write_1h + 2 * son.read) / 1e6)


def test_cost_state_is_reported(tmp_path: Path) -> None:
    t = steady(Transcript(), 2).cost(0.12).cost(0.34)
    assert load_session(t.write(tmp_path)).cost_usd == pytest.approx(0.34)


def test_params_change_is_proven(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=30_000, read=0)
    t.user("b").request(write=29_000, read=1_000)
    path = t.write(tmp_path)
    lines = path.read_text().splitlines()
    idx = [i for i, ln in enumerate(lines) if '"type": "api-request"' in ln][1]
    lines[idx] = lines[idx].replace('"params": {"model": "claude-sonnet-5-5"}',
                                    '"params": {"model": "claude-sonnet-5-5", "max_tokens": 1, '
                                    '"output_config": {"effort": "high"}}')
    idx0 = [i for i, ln in enumerate(lines) if '"type": "api-request"' in ln][0]
    lines[idx0] = lines[idx0].replace('"params": {"model": "claude-sonnet-5-5"}',
                                      '"params": {"model": "claude-sonnet-5-5", "max_tokens": 9, '
                                      '"output_config": {"effort": "medium"}}')
    path.write_text("\n".join(lines) + "\n")
    (b,) = analyze(load_session(path)).breaks
    assert b.cause == PARAMS and b.proven
    assert b.evidence[0].detail == "output_config: {'effort': 'medium'} -> {'effort': 'high'}"


def test_session_cost_uses_each_requests_model(tmp_path: Path) -> None:
    t = Transcript().user("a").request(write=1_000_000, read=0, output=0, inp=0)
    t.user("b").request(write=0, read=1_000_000, output=0, inp=0, model="claude-haiku-4-5")
    r = report(t, tmp_path)
    assert r.total_usd == pytest.approx(TABLE["sonnet-5.5"].write_1h + TABLE["haiku-4.5"].read)
    assert r.unpriced == []
