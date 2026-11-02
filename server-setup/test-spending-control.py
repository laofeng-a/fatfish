import importlib.util,json,tempfile,unittest,os
from pathlib import Path
from unittest.mock import patch
from activity_budget import ActivityLedger
from spending_control import limits_enabled
spec=importlib.util.spec_from_file_location('spending_gateway',Path(__file__).with_name('budget-gateway.py'))
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)

class SpendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.control=self.path/'control.json'
        p=patch.dict('os.environ',{'FATFISH_SPENDING_CONTROL':str(self.control)});p.start();self.addCleanup(p.stop)
    def mode(self,mode):self.control.write_text(json.dumps({'mode':mode}))
    def test_disable_and_restore_preserve_accounting(self):
        ledger=gateway.Ledger(self.path/'ledger.sqlite')
        ledger.reserve('2026-10-22',10,limit=10)
        with self.assertRaises(gateway.Denied):ledger.reserve('2026-10-22',1,limit=10)
        self.mode('accounting_only');self.assertFalse(limits_enabled())
        ledger.reserve('2026-10-22',11,limit=10)
        self.mode('enforced')
        with self.assertRaises(gateway.Denied):ledger.reserve('2026-10-22',1,limit=10)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(ledger.path)) as db:self.assertEqual(db.execute('SELECT SUM(charged) FROM calls').fetchone()[0],21)
    def test_background_ledger_still_records_without_cap(self):
        ledger=ActivityLedger(self.path/'background.sqlite')
        self.mode('accounting_only');ledger.reserve(3_000_000,'test')
        self.assertEqual(ledger.used(),3_000_000)
        self.mode('enforced')
        with self.assertRaises(ValueError):ledger.reserve(1,'test')
    def test_output_token_shape_limit_is_unchanged(self):
        self.mode('accounting_only')
        value={'model':gateway.MODEL,'messages':[{'role':'user','content':'hi'}],'max_tokens':8193}
        with self.assertRaises(gateway.Denied):gateway.prepare_request(value)
    @unittest.skipIf(os.name=='nt','Root-only CLI is exercised on target Linux')
    def test_one_command_restore_and_repeated_off_preserve_original_mode(self):
        import contextlib,io
        spec=importlib.util.spec_from_file_location('spending_cli',Path(__file__).with_name('fatfish-spending.py'))
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        real_path=Path
        with patch.object(cli,'Path',side_effect=lambda value:real_path(self.path)/real_path(value).name),patch.object(cli.os,'geteuid',return_value=0),patch.object(cli.os,'chown'),contextlib.redirect_stdout(io.StringIO()):
            for command in ('off','off'):
                with patch.object(cli.sys,'argv',['fatfish-spending',command]):cli.main()
            self.assertEqual(json.loads((self.path/'picoclaw-spending-control.json').read_text())['mode'],'accounting_only')
            with patch.object(cli.sys,'argv',['fatfish-spending','restore']):cli.main()
            self.assertFalse((self.path/'picoclaw-spending-control.json').exists())

if __name__=='__main__':unittest.main()
