"""The online Codex connector restores media without starting duplicate readers."""
import importlib.util
import json
import msvcrt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec=importlib.util.spec_from_file_location('worker_media',Path(__file__).with_name('worker.py'))
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)


class MediaAutostartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.config={'state_file':str(self.root/'status.json')}
        self.state=self.root/'video-status.json'
        (self.root/'media_connector.py').touch()
        (self.root/'video-reader.json').write_text(json.dumps({'python':str(self.root/'python.exe'),'state_file':str(self.state)}))
    def test_configured_stopped_reader_is_started_windowless(self):
        (self.root/'pythonw.exe').touch()
        with patch.object(worker.subprocess,'Popen',return_value=Mock(pid=123)) as spawn:
            self.assertEqual(worker.ensure_video_reader(self.config),{'state':'started','pid':123})
        command=spawn.call_args.args[0]
        self.assertEqual(command[0],str(self.root/'pythonw.exe'))
        self.assertEqual(command[1],str(self.root/'media_connector.py'))
        self.assertEqual(spawn.call_args.kwargs['creationflags'],worker.HIDDEN)
    def test_live_profile_lock_prevents_duplicate_process(self):
        with self.state.with_suffix('.lock').open('w+b') as lock:
            lock.write(b'0');lock.flush();lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            try:
                with patch.object(worker.subprocess,'Popen') as spawn:
                    self.assertEqual(worker.ensure_video_reader(self.config)['state'],'already_running')
                    spawn.assert_not_called()
            finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    def test_unconfigured_or_bad_media_does_not_break_codex(self):
        (self.root/'video-reader.json').write_text('broken json')
        self.assertEqual(worker.ensure_video_reader(self.config)['state'],'start_failed')
        (self.root/'video-reader.json').unlink()
        self.assertEqual(worker.ensure_video_reader(self.config)['state'],'not_configured')


if __name__=='__main__':unittest.main()
