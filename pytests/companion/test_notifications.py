import json
import sqlite3

from src.phone_edge.notification_queue import read_notifications
from src.mobile_agent.notification_router import NotificationInbox


def event(seq, key, title, text, fields=None):
    return dict(seq=seq,event_uuid=f'uuid-{seq}',package='com.tencent.mm',android_user_id=0,
                key=key,notification_id=123,event='onNotificationPosted',
                fields={'android.title':title,'android.text':text},intent_fields=fields or {})


def test_same_title_different_notifications_do_not_merge(tmp_path):
    inbox = NotificationInbox(str(tmp_path/'inbox.db'))
    a=event(1,'key-a','同名','A1')
    b=event(2,'key-b','同名','B1')
    inbox.ingest('edge',a)
    inbox.ingest('edge',b)
    inbox.ingest('edge',a)
    assert inbox.db.execute('SELECT count(*) FROM notification_inbox').fetchone()[0] == 2
    assert inbox.db.execute('SELECT count(DISTINCT route_key) FROM notification_inbox').fetchone()[0] == 2
    assert inbox.cursor('edge') == 2
    assert inbox.db.execute('SELECT chat_id FROM notification_inbox').fetchone()[0] is None
    inbox.db.close()


def test_updates_preserved_and_intent_identity_is_candidate(tmp_path):
    path=str(tmp_path/'inbox.db')
    inbox=NotificationInbox(path)
    inbox.ingest('edge',event(1,'a','A','A1',{'Chat_User':'wxid_abc'}))
    inbox.ingest('edge',event(2,'a','A','A2',{'Chat_User':'wxid_abc'}))
    assert inbox.db.execute('SELECT count(DISTINCT route_key) FROM notification_inbox').fetchone()[0] == 1
    assert inbox.db.execute('SELECT identity_status FROM notification_inbox').fetchone()[0] == 'intent_chat_user'
    inbox.db.close()
    restored=NotificationInbox(path)
    assert restored.cursor('edge') == 2
    restored.db.close()


def test_reconciliation_does_not_duplicate_conversation_observation(tmp_path):
    inbox=NotificationInbox(str(tmp_path/'inbox.db'))
    first=event(1,'a','A','[3条]paff: B2',{'Chat_User':'wxid_a','notification_msg_id':123})
    first['post_time']=100
    second=dict(first,seq=2,event_uuid='uuid-2',event='active_snapshot')
    inbox.ingest('edge',first)
    inbox.ingest('edge',second)
    assert inbox.db.execute('SELECT count(*) FROM notification_inbox').fetchone()[0] == 2
    assert inbox.db.execute('SELECT count(*) FROM conversation_notifications').fetchone()[0] == 1
    assert inbox.db.execute('SELECT coverage FROM conversation_notifications').fetchone()[0]=='merged_latest_only'
    inbox.db.close()


def test_edge_journal_pagination_survives_reopen(tmp_path):
    path=str(tmp_path/'queue.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE notification_events(seq INTEGER PRIMARY KEY,payload TEXT)')
        for i in range(1,4):
            db.execute('INSERT INTO notification_events VALUES(?,?)',(i,json.dumps({'event_uuid':str(i)})))
    first=read_notifications(path,0,2)
    second=read_notifications(path,first['next_cursor'],2)
    assert [x['seq'] for x in first['events']+second['events']] == [1,2,3]
