import time
from pathlib import Path

from tuicodingagent.benchmark import aggregate, normalize_legacy_trace


def test_aggregate_counts_summary_bytes_for_old_and_new_traces(tmp_path: Path) -> None:
    events = [
        {
            "event_type": "compression",
            "input_tokens": 1000,
            "output_tokens": 100,
            "response_bytes": 0,
            "metadata": {},
        },
        {
            "event_type": "compression",
            "input_tokens": 800,
            "output_tokens": 90,
            "response_bytes": 120,
            "metadata": {"summary_tokens": 30, "summary_bytes": 120},
        },
    ]
    metrics = aggregate(
        events,
        tmp_path,
        time.time(),
        {"jsonl": 0, "artifact": 0, "summary": 0, "memory": 0},
    )
    assert metrics["context_tokens_before_compaction"] == 1800
    assert metrics["context_tokens_after_compaction"] == 190
    assert metrics["summary_tokens_total"] == 130
    assert metrics["summary_bytes_written"] == 520


def test_aggregate_uses_legacy_compression_llm_usage(tmp_path: Path) -> None:
    events = [
        {"event_type": "llm_request", "output_tokens": 30, "response_bytes": 120},
        {
            "event_type": "compression",
            "input_tokens": 1000,
            "output_tokens": 400,
            "response_bytes": 0,
            "metadata": {},
        },
    ]
    metrics = aggregate(
        events,
        tmp_path,
        time.time(),
        {"jsonl": 0, "artifact": 0, "summary": 0, "memory": 0},
    )
    assert metrics["summary_tokens_total"] == 30
    assert metrics["summary_bytes_written"] == 120


def test_normalize_legacy_trace_preserves_raw_events(tmp_path: Path) -> None:
    source = tmp_path / "workspace" / ".tuicodingagent" / "traces" / "events.jsonl"
    source.parent.mkdir(parents=True)
    raw = '{"event_type":"tool_call","session_id":"","agent_id":"","model":""}\n'
    source.write_text(raw, encoding="utf-8")
    manifest = normalize_legacy_trace(tmp_path, "session-1", "model-1")
    normalized = (tmp_path / "events_normalized.jsonl").read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == raw
    assert '"session_id":"session-1"' in normalized
    assert '"model":"model-1"' in normalized
    assert manifest["corrections"] == {"session_id": 1, "agent_id": 1, "model": 1}
