import hashlib
import os
import sqlite3
import time

import pytest

from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.truth_db import CompanionTruthDB
from src.phone_edge.media_store import MediaStore
from src.phone_edge.chat_identity import parse_chat_identity


def test_activity_chat_id_and_ambiguity():
    identity = parse_chat_identity('mArguments=Bundle[{Chat_User=45009933541@chatroom, Main_UnreadCount=0}]')
    assert identity['chat_id'] == '45009933541@chatroom' and identity['is_group_chat']
    assert not parse_chat_identity('Chat_User=wxid_friend,')['is_group_chat']
    for text in ('no identity', 'Chat_User=a, Chat_User=b,'):
        with pytest.raises(ValueError):
            parse_chat_identity(text)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'MicroMsg'
    account = root / ('a' * 32)
    (account / 'image2').mkdir(parents=True)
    (account / 'voice2').mkdir()
    downloads = tmp_path / 'Download'
    downloads.mkdir()
    return MediaStore(str(root), str(downloads), settle_seconds=0), account


def test_equal_timestamp_pagination_and_small_opaque_originals(store):
    media, account = store
    stamp = time.time_ns() - 10_000_000_000
    for name, content in [('a', b'\xff\xd8\xffsmall'), ('b', b'wxgf-original'), ('c', b'GIF89a-small')]:
        path = account / 'image2' / name
        path.write_bytes(content)
        os.utime(path, ns=(stamp, stamp))
    first = media.scan(limit=2)
    second = media.scan(first['next_cursor']['mtime_ns'], first['next_cursor']['after'], limit=2)
    assert first['has_more'] and len(second['items']) == 1
    assert len({x['key'] for x in first['items'] + second['items']}) == 3
    assert all(x['association'] == 'unresolved' for x in first['items'])
    assert first['items'][1]['representation'] == 'opaque_original'


def test_changed_file_and_traversal_rejected(store):
    media, account = store
    path = account / 'image2' / 'a'
    path.write_bytes(b'\xff\xd8\xfftest')
    entry = media.scan()['items'][0]
    arguments = {'account': entry['account'], 'kind': entry['kind'], 'relative_path': entry['relative_path'],
                 'mtime_ns': entry['mtime_ns'], 'ctime_ns': entry['ctime_ns'], 'size': entry['size']}
    data, digest = media.read(**arguments)
    assert digest == hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError):
        media.read(**dict(arguments, relative_path='../secret'))
    path.write_bytes(b'changed length')
    with pytest.raises(ValueError):
        media.read(**arguments)


def test_archive_idempotence_privacy_and_sql_boundary(tmp_path):
    db = CompanionTruthDB(':memory:')
    pipeline = AssetPipeline(db, str(tmp_path))
    metadata = {'relative_path': 'original.jpg', 'mime_type': 'image/jpeg'}
    first = pipeline.ingest(b'original', metadata, source_id='one')
    assert pipeline.ingest(b'original', metadata, source_id='one')['file_code'] == first['file_code']
    assert pipeline.original_path(first['file_code']).read_bytes() == b'original'
    assert pipeline.attachable_codes('some group') == []
    assert first['file_code'] not in db.build_planner_catalog_injection(is_group_chat=True, sender_trust_role='OWNER')
    with pytest.raises(ValueError):
        pipeline.ingest(b'changed', metadata, source_id='one')
    for sql in ('DROP TABLE user_status;', 'CREATE TABLE foreign_table(x);', 'DELETE FROM asset_sources;'):
        with pytest.raises((sqlite3.DatabaseError, ValueError)):
            db.execute_table_sql(sql)
    db.execute_table_sql("INSERT INTO user_status(key,current_value) VALUES('test','value');")
    assert db.conn.execute('SELECT COUNT(*) FROM user_status').fetchone()[0] == 1
    db.close()
