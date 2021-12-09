from .util import journalpump_initialized
from journalpump.journalpump import JournalPump, JournalReader, PumpReader
from journalpump.senders.base import MsgBuffer
from pathlib import Path
from typing import List, Optional

import json
import os
import pytest
import shutil
import subprocess
import threading


class LogFiles:
    """Emulates rotation of log files.

    journald rotates files using following algorithm:
    * Move user-1000.journal -> user-1000@....journal
    * Create new user-1000.journal
    * Delete old file
    """

    def __init__(self, destination: Path) -> None:
        orig_path = Path(__file__).parent / "data" / "rotated_logs"
        self._orig_log_files: List[Path] = list(reversed(sorted(orig_path.glob("*.journal"))))
        self._rotate_to: Optional[Path] = None
        self._destination: Path = destination
        self._current_log_files: List[Path] = []

    @property
    def log_files(self):
        return self._current_log_files.copy()

    def rotate(self):
        log_file = self._orig_log_files.pop()
        tail_file = self._destination / "user-1000.journal"

        if self._rotate_to:
            shutil.move(tail_file, self._rotate_to)
            self._current_log_files.insert(1, self._rotate_to)
        else:
            assert not self._current_log_files
            self._current_log_files.insert(0, self._destination / "user-1000.journal")

        shutil.copyfile(log_file, self._destination / "user-1000.journal")
        self._rotate_to = self._destination / log_file.name

    def remove(self, *, last: int):
        for _ in range(last):
            log_file = self._current_log_files.pop()
            log_file.unlink()
            if not self._current_log_files:
                self._rotate_to = None


def test_log_rotator(tmp_path):
    log_path = tmp_path / "logs"
    log_path.mkdir()
    log_file_handler = LogFiles(log_path)

    assert len(list(log_path.glob("*"))) == 0

    log_file_handler.rotate()

    log_files = [p.name for p in log_path.glob("*")]
    assert len(log_files) == 1
    assert "user-1000.journal" in log_files

    log_file_handler.rotate()

    log_files = [p.name for p in log_path.glob("*")]
    assert len(log_files) == 2
    assert "user-1000.journal" in log_files

    log_file_handler.remove(last=1)

    log_files = [p.name for p in log_path.glob("*")]
    assert len(log_files) == 1
    assert "user-1000.journal" in log_files


class _MsgBuffer(MsgBuffer):
    """Wrapper around MsgBuffer which allows to wait for messages."""

    def __init__(self) -> None:
        super().__init__()
        self.has_messages = threading.Event()
        self.wait_threshold: Optional[int] = None

    def add_item(self, *, item, cursor):
        super().add_item(item=item, cursor=cursor)
        if self.wait_threshold and len(self.messages) >= self.wait_threshold:
            self.has_messages.set()

    def get_items(self):
        res = super().get_items()
        self.wait_threshold = None
        self.has_messages.clear()
        return res

    def set_threshold(self, th: int) -> None:
        self.wait_threshold = th
        if len(self.messages) >= self.wait_threshold:
            self.has_messages.set()


class StubSender:
    field_filter = None
    extra_field_values = None

    def __init__(self, *args, **kwargs):  # pylint: disable=unused-argument
        self.msg_buffer = _MsgBuffer()

    def start(self):
        pass

    def request_stop(self):
        pass

    def refresh_stats(self, *args, **kwargs):  # pylint: disable=unused-argument
        pass

    def __call__(self, *args, **kwargs):  # pylint: disable=unused-argument
        return self

    def get_messages(self, count: int, timeout: int):
        self.msg_buffer.set_threshold(count)
        assert self.msg_buffer.has_messages.wait(timeout), f"Timeout. Total messages received: {len(self.msg_buffer)}, {self.msg_buffer.messages}"

        messages = self.msg_buffer.get_items()
        return [json.loads(m[0]) for m in messages]


@pytest.fixture(name="journal_log_dir")
def fixture_journal_log_dir(tmp_path):
    log_path = tmp_path / "logs"
    log_path.mkdir()
    return log_path


@pytest.fixture(name="journalpump_factory")
def fixture_journalpump_factory(mocker, tmp_path, journal_log_dir):
    pump_thread = None
    pump = None

    def _start_journalpump(sender, *, pump_conf=None):
        nonlocal pump_thread
        nonlocal pump
        pump_conf = pump_conf or {}

        mocker.patch.object(PumpReader, "has_persistent_files", return_value=True)
        mocker.patch.object(PumpReader, "has_runtime_files", return_value=True)
        mocker.patch.object(JournalReader, "sender_classes", {"stub_sender": sender})

        config_path = tmp_path / "journalpump.json"
        with open(config_path, "w") as fp:
            json.dump(
                {
                    "readers": {
                        "my-reader": {
                            "journal_path": str(journal_log_dir),
                            "initial_position": "head",
                            "senders": {
                                "test-sender": {
                                    "output_type": "stub_sender"
                                },
                            },
                            "searches": [{
                                "fields": {
                                    "MESSAGE": "Message.*",
                                },
                                "name": "test-messages",
                            }],
                        },
                    },
                    **pump_conf,
                },
                fp,
            )

        pump = JournalPump(str(config_path))
        pump.poll_interval = 100
        pump_thread = threading.Thread(target=pump.run)
        pump_thread.start()
        assert journalpump_initialized(pump)
        return pump

    yield _start_journalpump

    if pump_thread and pump:
        pump.running = False
        pump_thread.join(timeout=3)


def test_journalpump_rotated_files(journalpump_factory, journal_log_dir):
    stub_sender = StubSender()
    journalpump_factory(stub_sender)
    lf = LogFiles(journal_log_dir)
    lf.rotate()
    messages = stub_sender.get_messages(10, timeout=3)
    assert set(m["MESSAGE"] for m in messages) == {f"Message {i}" for i in range(0, 10)}

    lf.rotate()
    lf.rotate()

    messages = stub_sender.get_messages(20, timeout=3)
    assert set(m["MESSAGE"] for m in messages) == {f"Message {i}" for i in range(10, 30)}


@pytest.mark.parametrize("msg_buffer_max_length", [3, 5, 10])
def test_journalpump_rotated_files_threshold(journalpump_factory, journal_log_dir, msg_buffer_max_length):
    stub_sender = StubSender()

    pump = journalpump_factory(stub_sender, pump_conf={"msg_buffer_max_length": msg_buffer_max_length})

    lf = LogFiles(journal_log_dir)
    lf.rotate()

    reader: JournalReader = next(iter(pump.readers.values()))
    assert reader.msg_buffer_max_length == msg_buffer_max_length

    lf.rotate()
    lf.rotate()

    for it in range(30 // msg_buffer_max_length):
        messages = stub_sender.get_messages(msg_buffer_max_length, timeout=3)
        expected = {f"Message {i}" for i in range(it * msg_buffer_max_length, msg_buffer_max_length * (it + 1))}
        assert len(messages) == msg_buffer_max_length
        assert set(m["MESSAGE"] for m in messages) == expected


@pytest.mark.parametrize("size,num_messages", [(50, 1), (1000, 2)])
def test_journalpump_rotated_files_threshold_bytes(journalpump_factory, journal_log_dir, size, num_messages):
    stub_sender = StubSender()

    pump = journalpump_factory(stub_sender, pump_conf={"msg_buffer_max_bytes": size})

    lf = LogFiles(journal_log_dir)
    lf.rotate()
    lf.rotate()
    lf.rotate()

    reader: JournalReader = next(iter(pump.readers.values()))
    assert reader.msg_buffer_max_bytes == size

    for it in range(30 // num_messages):
        messages = stub_sender.get_messages(num_messages, timeout=3)
        expected = {f"Message {i}" for i in range(it * num_messages, num_messages * (it + 1))}
        assert len(messages) == num_messages
        assert set(m["MESSAGE"] for m in messages) == expected


def _lsof_is_file_open(filename):
    """Check if file is open using lsof"""
    # psutil doesn't show deleted files, but this exactly what we want to test
    result = (subprocess.check_output(["lsof", "-p", str(os.getpid()), "-w"]).decode().split("\n"))
    for line in result:
        print(line)
        if filename in line:
            return True
    return False


@pytest.mark.skipif(not shutil.which("lsof"), reason="lsof is not available")
def test_journalpump_rotated_files_deletion(journalpump_factory, journal_log_dir):
    stub_sender = StubSender()
    journalpump_factory(stub_sender, pump_conf={"msg_buffer_max_length": 1})

    def _check_message(message):
        messages = stub_sender.get_messages(1, timeout=3)
        assert len(messages) == 1
        assert messages[0]["MESSAGE"] == message

    lf = LogFiles(journal_log_dir)
    lf.rotate()

    _check_message("Message 0")

    lf.rotate()

    # Loop once to get new files open
    _check_message("Message 1")

    log_files = lf.log_files
    assert len(log_files) == 2

    for filename in log_files:
        assert _lsof_is_file_open(filename.name)

    lf.remove(last=1)

    # Took buffered message
    _check_message("Message 2")
    # Dropped messages 3-9 as file was deleted
    _check_message("Message 10")

    assert not _lsof_is_file_open(log_files[-1].name)
