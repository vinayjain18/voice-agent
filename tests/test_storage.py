"""Lead CSV and transcript persistence."""

from __future__ import annotations

import csv

from voice_agent.storage import LEAD_FIELDS, append_lead, save_transcript


def test_lead_written_with_header(tmp_path):
    f = tmp_path / "leads.csv"
    append_lead(f, name="Asha", contact="asha@x.com", reason="wants a website")
    rows = list(csv.DictReader(f.open()))
    assert len(rows) == 1
    assert rows[0]["name"] == "Asha"
    assert rows[0]["timestamp_utc"]
    assert list(rows[0].keys()) == LEAD_FIELDS


def test_header_written_once_across_appends(tmp_path):
    f = tmp_path / "leads.csv"
    append_lead(f, name="A", contact="a@x.com", reason="one")
    append_lead(f, name="B", contact="b@x.com", reason="two")
    assert f.read_text().count("timestamp_utc") == 1
    assert len(list(csv.DictReader(f.open()))) == 2


def test_unknown_fields_are_ignored(tmp_path):
    f = tmp_path / "leads.csv"
    append_lead(f, name="A", contact="a@x.com", reason="r", not_a_column="x")
    rows = list(csv.DictReader(f.open()))
    assert "not_a_column" not in rows[0]


def test_commas_and_newlines_survive_round_trip(tmp_path):
    """A caller saying a comma must not shift every later column."""
    f = tmp_path / "leads.csv"
    append_lead(f, name="Rao, Priya", contact="p@x.com", reason="wants A, B and C")
    rows = list(csv.DictReader(f.open()))
    assert rows[0]["name"] == "Rao, Priya"
    assert rows[0]["reason"] == "wants A, B and C"


def test_append_never_raises_on_bad_path(tmp_path):
    """Losing a lead is bad; crashing a live call is worse."""
    bad = tmp_path / "nope.csv" / "leads.csv"  # parent is a file-shaped path
    bad.parent.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "nope.csv").write_text("blocking file")
    append_lead(bad, name="A", contact="a@x.com", reason="r")  # must not raise


class _History:
    def to_dict(self, **kwargs):
        return {"items": [{"role": "user", "content": "hello"}]}


def test_transcript_written(tmp_path):
    p = save_transcript(tmp_path / "t", room="call-1", history=_History())
    assert p is not None and p.exists()
    assert "call-1" in p.read_text()


def test_transcript_room_name_is_sanitised(tmp_path):
    p = save_transcript(tmp_path / "t", room="call-/../etc", history=_History())
    assert p is not None
    assert "/" not in p.name and ".." not in p.name
