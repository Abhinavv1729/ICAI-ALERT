import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage import Store
from monitor import batch_key


def sample(number='NASHIK-100', seats='20'):
    return {'course':'ICITSS – Orientation','region':'Western Region','pou':'Nashik',
            'fields':{'Batch Number':number,'Start Date':'20/11/2026','Seats Available':seats},'links':[]}


def test_baseline_new_and_delivery(tmp_path):
    db=Store(tmp_path/'state.db')
    s1=db.scan_start()
    assert db.scan_success(s1,[sample()],batch_key)==0
    assert db.snapshot()['stats']['known']==1
    assert db.pending_alerts()==[]

    s2=db.scan_start()
    assert db.scan_success(s2,[sample(seats='12'),sample('NASHIK-101')],batch_key)==1
    data=db.snapshot()
    assert data['stats']=={'known':2,'new':1,'pending':1}
    assert len(db.pending_alerts())==1
    assert db.pending_alerts()[0]['fields']['Batch Number']=='NASHIK-101'
    db.mark_notified(db.pending_alerts()[0]['key'])
    assert db.snapshot()['stats']['pending']==0
    s3=db.scan_start()
    assert db.scan_success(s3,[sample(seats='3'),sample('NASHIK-101')],batch_key)==0
    assert db.snapshot()['stats']['new']==1


def test_failed_scan_does_not_create_baseline(tmp_path):
    db=Store(tmp_path/'state.db')
    s1=db.scan_start()
    db.scan_failed(s1,'Page temporarily unavailable')
    assert db.snapshot()['last_success'] is None
    s2=db.scan_start()
    assert db.scan_success(s2,[sample()],batch_key)==0
    assert db.snapshot()['stats']['new']==0


def test_removed_batch_not_currently_listed(tmp_path):
    db=Store(tmp_path/'state.db')
    db.scan_success(db.scan_start(),[sample()],batch_key)
    db.scan_success(db.scan_start(),[],batch_key)
    batch=db.snapshot()['batches'][0]
    assert batch['currently_listed'] is False
