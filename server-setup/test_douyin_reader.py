import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import douyin_reader as reader

URL='https://www.douyin.com/video/7673779697926294825'
INFO={'id':'7673779697926294825','title':'公开标题','uploader':'公开作者','duration':12.5}


class Downloader:
    def __init__(self,options,info,download=False):
        self.options=options
        self.info=info
        self.download=download
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def extract_info(self,url,download=False):
        if download:self.process_ie_result(self.info,download=True)
        return dict(self.info)
    def process_ie_result(self,info,download=False):
        if download:
            path=Path(self.options['outtmpl'].replace('%(id)s',info['id']).replace('%(ext)s','m4a'))
            path.write_bytes(b'fake audio')


class DouyinTests(unittest.TestCase):
    def test_share_normalization_and_scope(self):
        self.assertEqual(reader.normalize_share('分享 '+URL+'，复制打开')['video_id'],INFO['id'])
        for value in ('http://www.douyin.com/video/7673779697926294825',
                      'https://www.douyin.com.evil.test/video/7673779697926294825',
                      'https://user:password@www.douyin.com/video/7673779697926294825',
                      'https://www.douyin.com/user/123',
                      URL+' https://www.douyin.com/video/7680841082151157433'):
            with self.subTest(value=value),self.assertRaises(reader.VideoReadError):
                reader.normalize_share(value)

    def test_unconnected_reader_does_not_claim_content(self):
        result=reader.DouyinReader().call({'url':URL,'mode':'transcript'})
        self.assertEqual(result['state'],'unavailable')
        self.assertNotIn('transcript',result)

    def test_result_is_bounded_utf8_and_has_no_raw_fields(self):
        raw={'state':'ok','source_url':URL,'transcript':'中文\n"'*20000,
             'description':'文'*4000,'signed_url':'https://private.invalid/?secret=1',
             'cookie':'credential','local_path':'private'}
        result=reader.project_result(raw)
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode()),reader.MAX_RESULT_BYTES)
        self.assertTrue(result['transcript_truncated'])
        self.assertTrue(result['transcript'])
        for key in ('signed_url','cookie','local_path'):self.assertNotIn(key,result)

    def test_cookie_copy_preserves_original_and_cleans_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie=Path(directory)/'source.txt'
            original=b'\xef\xbb\xbf# test cookie fixture\n'
            cookie.write_bytes(original)
            worker=reader.LocalVideoWorker(directory,cookie)
            with worker.cookie_copy(directory) as copy:
                self.assertNotEqual(Path(copy),cookie)
                self.assertFalse(Path(copy).read_bytes().startswith(b'\xef\xbb\xbf'))
            self.assertEqual(cookie.read_bytes(),original)
            self.assertFalse(Path(copy).exists())

    def test_metadata_does_not_transcribe(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=reader.LocalVideoWorker(directory,
                extractor_factory=lambda options:Downloader(options,INFO),
                transcriber=lambda _:self.fail('metadata loaded ASR'))
            result=worker.read({'url':URL,'mode':'metadata'})
            self.assertEqual(result['state'],'ok')
            self.assertFalse(result['capabilities']['audio_transcript'])

    def test_wrong_id_and_duration_are_rejected(self):
        for info in (dict(INFO,id='7680841082151157433'),dict(INFO,duration=None),dict(INFO,duration=601)):
            with self.subTest(info=info),tempfile.TemporaryDirectory() as directory:
                worker=reader.LocalVideoWorker(directory,
                    extractor_factory=lambda options:Downloader(options,info),
                    transcriber=lambda _:self.fail('invalid media reached ASR'))
                with self.assertRaises(reader.VideoReadError):worker.read({'url':URL,'mode':'transcript'})

    def test_browser_fallback_downloads_and_saves_full_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript='实际音轨文字。'*8000
            calls=[]
            def extractor(options):
                calls.append(options)
                if options['skip_download']:raise RuntimeError('raw private error must not escape')
                return Downloader(options,INFO)
            worker=reader.LocalVideoWorker(directory,extractor_factory=extractor,
                transcriber=lambda audio:transcript,output_directory=Path(directory)/'output')
            with patch.object(worker,'_browser_metadata',return_value=dict(INFO)):
                result=worker.read({'url':URL,'mode':'transcript'})
            saved=Path(directory)/'output'/(INFO['id']+'.fatfish.md')
            self.assertIn(transcript,saved.read_text(encoding='utf-8'))
            self.assertTrue(result['transcript_saved_locally'])
            self.assertTrue(result['transcript_truncated'])
            self.assertEqual(result['transcript_source'],'local_whisper_asr')
            self.assertFalse(result['capabilities']['video_frames'])
            self.assertEqual(calls[1]['format'],'bestaudio/best')

    def test_worker_cli_stdout_is_one_safe_json_line(self):
        script=Path(__file__).with_name('douyin-worker.py')
        child=subprocess.run([sys.executable,str(script)],input=b'{malformed',capture_output=True,timeout=10)
        self.assertEqual(child.stderr,b'')
        self.assertEqual(len(child.stdout.splitlines()),1)
        self.assertEqual(json.loads(child.stdout)['reason'],'invalid_request')

    def test_browser_failure_hides_child_output(self):
        child=subprocess.CompletedProcess([],1,'private raw stdout','private raw stderr')
        worker=reader.LocalVideoWorker('.',browser_node='node',playwright_module='module')
        with patch.object(subprocess,'run',return_value=child):
            with self.assertRaises(reader.VideoReadError) as error:worker._browser_metadata(INFO['id'])
        self.assertEqual(error.exception.code,'douyin_public_page_unavailable')
        self.assertNotIn('private',str(error.exception))


if __name__=='__main__':unittest.main()
