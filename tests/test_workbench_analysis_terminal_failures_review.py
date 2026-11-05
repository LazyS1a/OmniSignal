"""Independent local reproductions; synthetic content, no provider/network retry.

These guard reproduced stranded-running paths and constructor recovery.
"""
from copy import deepcopy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from omnisignal.workbench.analysis import AnalysisService, LOGGER
from test_workbench_analysis import FakeProvider, SUGGESTION, prepared as analysis_prepared


@pytest.fixture(autouse=True)
def isolated_workbench_logger(monkeypatch):
    # Other suites run Alembic fileConfig(disable_existing_loggers=True) in the
    # pytest process after collection has imported this module. The standalone
    # workbench does not run Alembic; restore its fresh-process logger state for
    # these capture assertions, and undo the test-local override afterwards.
    monkeypatch.setattr(LOGGER, 'disabled', False)


def test_normal_uvicorn_process_emits_safe_terminal_event():
    script='import sys\nsys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]/'src'))+')\n'+"""import uvicorn
from omnisignal.workbench.analysis import LOGGER,_terminal_error
uvicorn.Config('omnisignal.workbench.app:create_app',factory=True)
assert LOGGER.disabled is False
_terminal_error('terminal_write_failed',ValueError('synthetic-private-response'))
"""
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0
    assert 'analysis_event=terminal_write_failed error_type=ValueError' in result.stderr
    assert 'synthetic-private-response' not in result.stdout+result.stderr


def current_row(store, job_id):
    with store.db() as db:
        return dict(db.execute('SELECT * FROM analysis_jobs WHERE id=?',(job_id,)).fetchone())


def recovery_without_call_or_permission_change(store, asset, provider, job_id):
    with store.db() as db:
        before=json.loads(db.execute('SELECT document FROM assets WHERE id=?',(asset['id'],)).fetchone()[0])
        usage_before=[tuple(row) for row in db.execute('SELECT * FROM analysis_usage')]
        audit_before=db.execute('SELECT count(*) FROM audit').fetchone()[0]
    calls=provider.calls
    # Constructor recovery only; never start() or run_pending().
    AnalysisService(store,provider)
    row=current_row(store,job_id)
    assert row['status']=='interrupted' and row['finished_at'] and row['charged']==1
    assert provider.calls==calls==1
    with store.db() as db:
        after=json.loads(db.execute('SELECT document FROM assets WHERE id=?',(asset['id'],)).fetchone()[0])
        assert after=={**before,'analysis_status':'not_analyzed'}
        assert [tuple(row) for row in db.execute('SELECT * FROM analysis_usage')]==usage_before
        assert db.execute('SELECT count(*) FROM audit').fetchone()[0]==audit_before+1
        assert db.execute("SELECT count(*) FROM audit WHERE action='analysis_interrupted'").fetchone()[0]==1


def test_unpaired_surrogate_fails_schema_before_terminal_write_without_retry(analysis_prepared):
    store,asset=analysis_prepared
    output=deepcopy(SUGGESTION)
    output['reason']='Synthetic invalid Unicode '+chr(0xD800)
    provider=FakeProvider(lambda *_:output)
    service=AnalysisService(store,provider)
    job=service.enqueue(asset['id'],asset['revision'],'synthetic-admin')
    result=service.run_pending()
    assert result['status']=='failed' and result['failure_code']=='response_schema_invalid'
    row=current_row(store,job['id'])
    assert row['status']=='failed' and row['finished_at'] and row['charged']==1
    assert row['suggestion'] is None
    assert service.run_pending() is None and provider.calls==1
    assert store.asset(asset['id'])['analysis_status']=='not_analyzed'


def test_presentation_exception_cannot_roll_back_committed_result(analysis_prepared,monkeypatch,caplog):
    store,asset=analysis_prepared
    provider=FakeProvider();service=AnalysisService(store,provider)
    job=service.enqueue(asset['id'],asset['revision'],'synthetic-admin')
    original=service._job
    def fail_after_finish(row,db=None):
        if row['status']=='completed':raise ValueError('synthetic presentation failure')
        return original(row,db)
    monkeypatch.setattr(service,'_job',fail_after_finish)
    result=service.run_pending()
    assert result['status']=='completed' and result['presentation_unavailable'] is True
    row=current_row(store,job['id'])
    assert row['status']=='completed' and row['finished_at'] and row['suggestion']
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='analysis_completed'").fetchone()[0]==1
    assert provider.calls==1
    assert 'terminal_presentation_failed' in caplog.text
    assert 'synthetic presentation failure' not in caplog.text


def test_direct_terminal_write_failure_marks_interrupted_without_second_provider_call(analysis_prepared,monkeypatch,caplog):
    store,asset=analysis_prepared
    provider=FakeProvider();service=AnalysisService(store,provider)
    job=service.enqueue(asset['id'],asset['revision'],'synthetic-admin')
    original=service._finish
    def fail_first(db,job,status,reason,suggestion=None):
        if status=='completed':raise sqlite3.OperationalError('synthetic-private-driver-details')
        return original(db,job,status,reason,suggestion)
    monkeypatch.setattr(service,'_finish',fail_first)
    result=service.run_pending()
    assert result['status']=='interrupted' and result['failure_code']=='result_persistence'
    assert current_row(store,job['id'])['charged']==1 and service.configuration()['used']==1
    assert service.run_pending() is None and provider.calls==1
    assert 'terminal_write_failed error_type=OperationalError' in caplog.text
    assert 'synthetic-private-driver-details' not in caplog.text


def test_persistent_local_write_failure_reports_unwritten_marker_and_constructor_never_retries(analysis_prepared,monkeypatch,caplog):
    store,asset=analysis_prepared
    provider=FakeProvider();service=AnalysisService(store,provider)
    job=service.enqueue(asset['id'],asset['revision'],'synthetic-admin')
    def cannot_write(*args,**kwargs):raise sqlite3.OperationalError('synthetic-private-driver-details')
    monkeypatch.setattr(service,'_finish',cannot_write)
    assert service.run_pending() is None
    assert current_row(store,job['id'])['status']=='running'
    assert service.run_pending() is None and provider.calls==1
    assert 'terminal_marker_unwritten error_type=OperationalError' in caplog.text
    assert 'synthetic-private-driver-details' not in caplog.text
    recovery_without_call_or_permission_change(store,asset,provider,job['id'])


def test_remote_terminal_write_failure_keeps_original_identity_for_read_only_reconcile(analysis_prepared,monkeypatch):
    store,asset=analysis_prepared
    class RemoteProvider(FakeProvider):
        is_nova=True
        def analyze(self,asset,image,*,receipt):
            receipt('synthetic-remote-id')
            return super().analyze(asset,image)
    provider=RemoteProvider();service=AnalysisService(store,provider)
    job=service.enqueue(asset['id'],asset['revision'],'synthetic-admin')
    original=service._finish
    def fail_first(db,job,status,reason,suggestion=None):
        if status=='completed':raise sqlite3.IntegrityError('synthetic-private-driver-details')
        return original(db,job,status,reason,suggestion)
    monkeypatch.setattr(service,'_finish',fail_first)
    result=service.run_pending();row=current_row(store,job['id'])
    assert result['status']=='waiting_remote' and row['finished_at'] is None
    assert row['remote_request_id'] and row['remote_job_id']=='synthetic-remote-id'
    assert row['charged']==1 and row['next_poll_at']>0 and service.configuration()['used']==1
    assert service.run_pending() is None and provider.calls==1
