import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock,patch
import emote_send


class RuntimeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.path=root/'photo.png';self.path.write_bytes(b'fixture')
        config=root/'config.json';config.write_text(json.dumps({'channel_list':{'telegram':{'settings':{'token':'fixture'}}}}))
        p=patch.object(emote_send,'CONFIG',config);p.start();self.addCleanup(p.stop)
        e=patch.dict(os.environ,{'FATFISH_TOOL_MESSAGE_ID':'heartbeat:wake','FATFISH_TOOL_SESSION_KEY':'agent:autonomy-3','FATFISH_TOOL_CHAT_ID':'123'});e.start();self.addCleanup(e.stop)

    def response(self):
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'ok':True,'result':{'message_id':91,'photo':[{'file_id':'photo'}]}}).encode()
        return response

    def test_blocked_origin_never_calls_telegram(self):
        with patch.object(emote_send,'runtime_rpc',return_value={'allowed':True,'skip':True,'reason':'explicit_internal_note'})as rpc,patch.object(emote_send.urllib.request,'urlopen')as send:
            result=emote_send.send(self.path,'123')
        self.assertFalse(result['newly_sent']);send.assert_not_called()
        self.assertEqual(rpc.call_args.args[1]['message_id'],'heartbeat:wake')

    def test_actual_send_records_exact_receipt(self):
        with patch.object(emote_send,'runtime_rpc',side_effect=[{'allowed':True,'ticket':'real-ticket'},{'allowed':True}])as rpc,patch.object(emote_send.urllib.request,'urlopen',return_value=self.response()):
            result=emote_send.send(self.path,'123')
        self.assertEqual(result['message_id'],91)
        self.assertEqual(rpc.call_args_list[-1].args[0],'complete')
        self.assertEqual(rpc.call_args_list[-1].args[1]['message_ids'],['91'])

    def test_unknown_send_is_not_recorded_as_sent(self):
        with patch.object(emote_send,'runtime_rpc',side_effect=[{'allowed':True,'ticket':'ticket'},{'allowed':True}])as rpc,patch.object(emote_send.urllib.request,'urlopen',side_effect=TimeoutError):
            with self.assertRaises(ValueError):emote_send.send(self.path,'123')
        self.assertEqual(rpc.call_args_list[-1].args[1]['status'],'unknown')

    def test_receipt_failure_preserves_actual_message_id(self):
        with patch.object(emote_send,'runtime_rpc',side_effect=[{'allowed':True,'ticket':'ticket'},ValueError('offline')]),patch.object(emote_send.urllib.request,'urlopen',return_value=self.response()):
            result=emote_send.send(self.path,'123')
        self.assertEqual(result['message_id'],91);self.assertTrue(result['newly_sent'])
        self.assertEqual(result['runtime_receipt'],'unconfirmed')

    def test_duplicate_ticket_never_resends(self):
        with patch.object(emote_send,'runtime_rpc',return_value={'allowed':True,'skip':True,'ticket':'ticket','send_status':'sent'}),patch.object(emote_send.urllib.request,'urlopen')as send:
            result=emote_send.send(self.path,'123')
        self.assertEqual(result['status'],'already_sent');send.assert_not_called()


if __name__=='__main__':unittest.main()
