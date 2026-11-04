"""Task closure works without another verification request or a desktop worker."""
import json
from pathlib import Path
import tempfile
import unittest
from runtime_policy import Runtime


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]')
        cfg=root/'settings.json';cfg.write_text(json.dumps({'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json')}))
        self.r=Runtime(cfg)
        self.ctx={'channel':'telegram','chat_id':'123','message_id':'1','session_key':'telegram:123'}

    def seed(self,bridge,identifier):
        with bridge.r.db() as db:
            db.execute("INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,'needs_review',1,1)",(identifier,'0123456789abcdef',identifier))
            db.execute("INSERT INTO codex_jobs(id,source,fingerprint,action,args,state,claim,created,updated,output,chain_id,auto_reply) VALUES(?,?,?,'send','{}','queued','',1,1,'',?,1)",(identifier+'-followup','test',identifier,identifier))

    def test_default_finish_closes_all_three_backends_and_cancels_queued_followup(self):
        for name,bridge in [('codex',self.r.codex),('dsh',self.r.dsh.bridge),('claude',self.r.claude.bridge)]:
            with self.subTest(backend=name):
                identifier=name+'-task';self.seed(bridge,identifier)
                result=bridge.chain_action('finish',{'job_id':identifier},self.ctx)
                self.assertEqual(result['state'],'completed')
                with bridge.r.db() as db:
                    self.assertEqual(db.execute('SELECT state FROM codex_chains WHERE id=?',(identifier,)).fetchone()[0],'completed')
                    self.assertEqual(db.execute('SELECT state FROM codex_jobs WHERE id=?',(identifier+'-followup',)).fetchone()[0],'cancelled')

    def test_explicit_outcome_is_preserved_and_invalid_outcome_is_rejected(self):
        for outcome in ['failed','stopped','invalid']:
            identifier='task-'+outcome;self.seed(self.r.codex,identifier)
            if outcome=='invalid':
                with self.assertRaises(ValueError):self.r.codex.chain_action('finish',{'job_id':identifier,'status':outcome},self.ctx)
            else:self.assertEqual(self.r.codex.chain_action('finish',{'job_id':identifier,'status':outcome},self.ctx)['state'],outcome)


if __name__=='__main__':unittest.main(verbosity=2)
