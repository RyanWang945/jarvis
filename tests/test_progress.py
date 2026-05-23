from __future__ import annotations

from app.progress import NoopProgressReporter, ProgressEvent, ProgressReporter


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []
        self.closed = False

    def on_progress(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class FailingSink:
    def on_progress(self, event: ProgressEvent) -> None:
        raise RuntimeError("sink failed")

    def close(self) -> None:
        raise RuntimeError("close failed")


def test_progress_reporter_fans_out_and_closes() -> None:
    first = RecordingSink()
    second = RecordingSink()
    reporter = ProgressReporter([first, second])

    reporter.emit("planning_started", turn_id=7, summary="planning")
    reporter.close()

    assert [event.event_type for event in first.events] == ["planning_started"]
    assert first.events[0].turn_id == 7
    assert [event.event_type for event in second.events] == ["planning_started"]
    assert first.closed is True
    assert second.closed is True


def test_progress_reporter_isolates_sink_failures() -> None:
    recorder = RecordingSink()
    reporter = ProgressReporter([FailingSink(), recorder])

    reporter.emit("node_started", node_id="main")
    reporter.close()

    assert [event.event_type for event in recorder.events] == ["node_started"]


def test_noop_progress_reporter_has_no_side_effects() -> None:
    reporter = NoopProgressReporter()

    reporter.emit("turn_started")
    reporter.close()
