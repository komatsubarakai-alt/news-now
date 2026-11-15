"""Characterize the unchanged production validator with existing fictional fixtures.

Passing characterization of the duplicate counterexample is NOT a safety pass.
"""
import copy,importlib.util,os,socket,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from .allocation import native

class NativeBoundaryTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[2]
        spec=importlib.util.spec_from_file_location('allocation_fictional_helpers',root/'tests/test_tracking.py');self.f=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.f)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);previous=os.getcwd();os.chdir(self.temp.name);self.addCleanup(os.chdir,previous)
        for target,name in [(socket.socket,'connect'),(native,'response')]:
            guard=patch.object(target,name,side_effect=AssertionError('network/API forbidden'));guard.start();self.addCleanup(guard.stop)
        self.events=[self.f.event()];self.article=self.f.article('容疑者を起訴','https://example.com/fictional-later',self.f.NOW.isoformat())
        self.group={'event_name':self.events[0]['event_name'],'category':'事件・事故','tracking_value':'high','articles':[self.article]}
    def test_valid_mock_followup_uses_existing_safety_and_deduplicates(self):
        answer=self.f.report(self.article);pending=self.f.merge_incoming(self.events,[self.group],self.f.FakeAI(answer),self.f.NOW)
        self.assertEqual(pending,[]);self.assertEqual(len(self.events),1);self.assertEqual(self.events[0]['progress_count'],1)
        self.f.merge_incoming(self.events,[self.group],self.f.FakeAI(),self.f.NOW);self.assertEqual(self.events[0]['progress_count'],1)
    def test_invalid_mock_evidence_is_held(self):
        before=copy.deepcopy(self.events);answer=self.f.report(self.article,evidence_urls=['https://example.com/not-input'])
        pending=self.f.merge_incoming(self.events,[self.group],self.f.FakeAI(answer),self.f.NOW)
        self.assertEqual(pending,[self.group]);self.assertEqual(self.events,before)
    def test_mock_budget_stop_preserves_group(self):
        before=copy.deepcopy(self.events);pending=self.f.merge_incoming(self.events,[self.group],self.f.FakeAI(native.BudgetExceeded('mock limit')),self.f.NOW)
        self.assertEqual(pending,[self.group]);self.assertEqual(self.events,before)
    def test_native_duplicate_new_event_counterexample_is_not_prevented(self):
        # Ground truth is same event by construction. AI answer is intentionally wrong.
        # The unchanged validator accepts this report despite an existing matching event.
        old=copy.deepcopy(self.events[0]);answer=self.f.report(self.article,relation='new_event',event_id='')
        pending=self.f.merge_incoming(self.events,[self.group],self.f.FakeAI(answer),self.f.NOW)
        self.assertEqual(len(self.events),2);self.assertEqual(pending,[]);self.assertEqual(self.events[0],old)
        self.assertNotEqual(self.events[0]['event_id'],self.events[1]['event_id'])

if __name__=='__main__':unittest.main()
