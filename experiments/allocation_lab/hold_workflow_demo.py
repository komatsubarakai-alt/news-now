"""Fictional-only demonstration, not a public site or real operator UI."""
import copy,json,socket,tempfile
from pathlib import Path
from unittest.mock import patch
from .hold_workflow import HoldWorkflow,MockIntent
from .test_hold_workflow import bundle,principal,NOW
from .allocation import native,PLANS
from .session import digest


def demo():
    fixtures={name:bundle('different' if name=='distinct' else 'same') for name in ('number_conflict','distinct','missing','unverified','archive','supplement','budget','completed')}
    fixtures['missing']['incoming']['facts'].pop('occurred_at');fixtures['supplement']['incoming']['facts'].pop('location');fixtures['unverified']['incoming']['citations'][0]['source_status']='unverified'
    with tempfile.TemporaryDirectory(prefix='operator-workflow-demo-') as directory,patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')),patch.object(native,'response',side_effect=AssertionError('AI forbidden')):
        workflow=HoldWorkflow(Path(directory)/'fictional.sqlite');operations=[]
        for name,value in fixtures.items():workflow.ingest('ingest-'+name,'fictional-'+name,value,'2026-10-01T00:00:00Z');workflow.local_check('fictional-'+name)
        before=workflow.tasks(NOW)['actionable_total']
        def act(name,action,data,label):
            case='fictional-'+name;rev=workflow.head(case)['revision'];result=workflow.act(label,case,rev,action,data,principal(),MockIntent(rev,action,digest(data)),NOW);operations.append({'case':name,'action':action,'result':result});return result
        def review(name,relation,label):return act(name,'review',{'relation':relation,'reason':'架空データの確認'},label)
        def publish(name,label):return act(name,'publish_lab',{'confirmed':True,'preview_hash':digest(workflow.preview('fictional-'+name))},label)
        review('number_conflict','distinct','review-conflict')
        review('distinct','distinct','review-distinct');publish('distinct','publish-distinct')
        for name in ('missing','unverified'):review(name,'hold','review-'+name)
        act('archive','archive',{'reason':'追加根拠がなく確認不能'},'archive')
        review('supplement','hold','hold-supplement');workflow.ingest('supplement-added','fictional-supplement',bundle(),NOW);review('supplement','same','review-supplement')
        act('budget','budget_pause',{},'pause');workflow.close();workflow=HoldWorkflow(Path(directory)/'fictional.sqlite');act('budget','budget_resume',{},'resume')
        review('completed','same','review-completed');publish('completed','publish-completed')
        queue=workflow.tasks(NOW);first=[workflow.local_check('fictional-'+name) for name in fixtures];second=[workflow.local_check('fictional-'+name) for name in fixtures]
        result={'data_kind':'fictional_only','initial_actionable_cases':before,'final_actionable_cases':queue['actionable_total'],
                'waiting_for_evidence_or_archived':len(queue['waiting_for_evidence']),'lab_visible_previews':sum(workflow.export_lab('fictional-'+name)['state']=='lab_visible' for name in fixtures),
                'accepted_mock_operator_commands':len(operations),'unchanged_local_rechecks_suppressed':sum(x['suppressed'] for x in second),
                'actual_human_minutes':'unmeasured','actual_smartphone_taps':'unmeasured','event_identity_decisions_applied':0,'production_publications':0,'actual_API_calls':0,'A_caps':PLANS['A'],
                'operations':operations,'manual_queue':queue}
        workflow.verify_audit();workflow.close();return result

if __name__=='__main__':print(json.dumps(demo(),ensure_ascii=False,indent=2))
