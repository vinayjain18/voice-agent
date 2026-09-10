"""Transcript persistence. Appointments live in Sheets, see test_appointments.py."""

from __future__ import annotations

from voice_agent.storage import save_transcript


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
