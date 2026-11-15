"""Report must preserve regressions, gaps and uncertainty without making up points."""
import json

from app.evolution.report import render_report


def test_report_shows_real_denominators_and_does_not_fill_skipped_rounds(tmp_path):
    baseline={'n':10,'correct':5,'score':.5,'coverage':1,'balanced_accuracy':.5,'always_up_accuracy':.5,'always_down_accuracy':.5}
    history=[{'round':0,'version':'g0','learning':{'score':.7},'validation':{'score':.4},'best_accuracy':.5,'accepted':False},
             {'round':1,'version':'g1','skipped':'insufficient_learning_evidence','best_accuracy':.5,'accepted':False}]
    result={'chosen':'baseline','test':{'baseline':baseline},'improvement_proven':False,
            'paired_test_comparison':{'paired_14_day_block_bootstrap_95ci':[-.1,.1]}}
    render_report(tmp_path,baseline=baseline,history=history,final=result)
    curve=json.loads((tmp_path/'curve.json').read_text())
    assert curve['points'][0]['validation_accuracy']==.4
    assert curve['points'][1]['validation_accuracy'] is None
    assert curve['points'][1]['best_accuracy']==.5
    document=(tmp_path/'report.html').read_text()
    assert '尚未证明' in document and '50.0%' in document and '10 条' in document
    assert 'paired_14_day_block_bootstrap_95ci' in document
    assert (tmp_path/'accuracy.png').stat().st_size>1000
    assert (tmp_path/'accuracy.svg').stat().st_size>1000
    assert not list(tmp_path.glob('*.tmp*'))
