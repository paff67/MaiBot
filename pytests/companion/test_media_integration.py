from types import SimpleNamespace
import hashlib
import json
import io

import httpx
import pytest

from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.truth_db import CompanionTruthDB
from src.mobile_agent.media_batches import MediaBatchCollector
from src.mobile_agent.wechat_live_bridge import WeChatLiveBridge
from src.mobile_agent.wechat_screen_parser import WeChatBubble, WeChatScreenState
from src.phone_edge.send_ledger import SendLedger
from src.mobile_agent.media_correlation import correlate_media_batch
from src.phone_edge.chat_identity import extract_media_references
from src.companion_vault.model_assets import build_original_parts


ACCOUNT = 'a' * 32
CHAT = '123@chatroom'
KEY = ACCOUNT + '/' + CHAT
DATA = b'\xff\xd8\xfffixture-original'
ITEM = dict(account=ACCOUNT, kind='image', key='image/ab/photo.jpg', relative_path='ab/photo.jpg',
            scan_ns=150, ctime_ns=150, mtime_ns=10, size=len(DATA), mime_type='image/jpeg')
PATH = f'/data/data/com.tencent.mm/MicroMsg/{ACCOUNT}/image2/ab/photo.jpg'
BUBBLE = WeChatBubble('OTHER', 'alice', 'image', '', (120, 300, 500, 500), (310, 400), 'msg-1')


def test_emoji_original_is_recallable_with_exact_path_evidence(tmp_path):
    item = dict(ITEM, kind='emoji', key='emoji/decrypt/hash', relative_path='decrypt/hash',
                mime_type='application/octet-stream', representation='opaque_original')
    path = f'/data/data/com.tencent.mm/MicroMsg/{ACCOUNT}/emoji/decrypt/hash'
    assert extract_media_references('filePath=' + path, ACCOUNT) == [path]
    assert correlate_media_batch([BUBBLE], [item], [path]) == [item]
    db = CompanionTruthDB(':memory:')
    assets = AssetPipeline(db, str(tmp_path))
    raw = b'wxgf-original-fixture'
    row = assets.ingest(raw, item, source_id='emoji-fixture')
    assets.bind_conversation('emoji-fixture', ACCOUNT, CHAT, 'exact path test')
    assert assets.original_path(row['file_code']).read_bytes() == raw
    assert assets.scoped_file_codes(KEY, [row['file_code']]) == [row['file_code']]
    assert '微信表情包' in db.conn.execute('SELECT distinctive_features FROM media_vault').fetchone()[0]
    db.close()


def test_gif_decodes_to_unscaled_uncompressed_png(tmp_path):
    from PIL import Image
    image = Image.new('RGB', (19, 23), (12, 34, 56))
    buffer = io.BytesIO()
    image.save(buffer, format='GIF')
    raw = buffer.getvalue()
    db = CompanionTruthDB(':memory:')
    assets = AssetPipeline(db, str(tmp_path))
    row = assets.ingest(raw, {'mime_type':'image/gif','relative_path':'emoji.gif'},source_id='gif')
    parts = build_original_parts(assets,row['file_code'],None)
    decoded = Image.open(io.BytesIO(parts[0].inline_data.data))
    assert decoded.size == image.size
    assert decoded.getpixel((0,0)) == (12,34,56,255)
    assert assets.original_path(row['file_code']).read_bytes() == raw
    meta = json.loads(db.conn.execute("SELECT metadata FROM asset_sources WHERE source_id=?", ('decoded-png:'+row['file_code']+':0',)).fetchone()[0])
    assert meta['png_compress_level'] == 0
    db.close()


def test_pending_candidates_never_download_or_auto_bind(tmp_path):
    db = CompanionTruthDB(':memory:')
    assets = AssetPipeline(db, str(tmp_path))
    calls = []
    def handle(request):
        calls.append(request.url.path)
        assert request.url.path == '/wechat/media_since'
        return httpx.Response(200, json={'cursor_version': 2, 'account': ACCOUNT, 'items': [ITEM], 'has_more': False})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        collector = MediaBatchCollector(assets, client, 'http://edge')
        codes, status = collector.collect([BUBBLE], dict(account=ACCOUNT, chat_id=CHAT, captured_at_ns=200), 100)
    assert codes == [] and status == 'pending_association_evidence'
    assert calls == ['/wechat/media_since']
    assert db.conn.execute('SELECT count(*) FROM asset_sources').fetchone()[0] == 0
    db.close()


def test_exact_path_retry_recovers_audio_derivative_and_batches(tmp_path):
    db = CompanionTruthDB(':memory:')
    assets = AssetPipeline(db, str(tmp_path))
    voice = dict(ITEM, kind='voice', key='voice/msg_x.amr', relative_path='msg_x.amr', mime_type='audio/x-silk')
    voice_path = f'/data/data/com.tencent.mm/MicroMsg/{ACCOUNT}/voice2/msg_x.amr'
    bubble = WeChatBubble('OTHER', 'alice', 'voice', '2秒', (0, 0, 1, 1), (0, 0), 'voice1')
    fail_audio = [True]
    downloads = []
    def handle(request):
        path = request.url.path
        if path.endswith('media_since'):
            return httpx.Response(200, json={'cursor_version': 2, 'account': ACCOUNT, 'items': [voice], 'has_more': False})
        if path.endswith('chat_identity'):
            return httpx.Response(200, json={'account': ACCOUNT, 'chat_id': CHAT})
        downloads.append(path)
        if path.endswith('media_wav') and fail_audio[0]:
            fail_audio[0] = False
            return httpx.Response(500)
        data = b'RIFFfixture' if path.endswith('media_wav') else DATA
        return httpx.Response(200, content=data, headers={'X-Content-SHA256': hashlib.sha256(data).hexdigest()})
    snapshot = dict(account=ACCOUNT, chat_id=CHAT, captured_at_ns=200, media_reference_paths=[voice_path])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        collector = MediaBatchCollector(assets, client, 'http://edge')
        with pytest.raises(httpx.HTTPStatusError):
            collector.collect([bubble], snapshot, 100)
        codes, status = collector.collect([bubble], snapshot, 100)
        assert status == 'verified' and len(codes) == 1
        assert collector.collect([bubble], snapshot, 100) == (codes, 'verified')
    assert downloads.count('/wechat/media_file') == 1
    assert downloads.count('/wechat/media_wav') == 2
    assert assets.scoped_file_codes(KEY, codes) == codes
    assert assets.scoped_file_codes(ACCOUNT + '/wrong', codes) == []
    db.close()


def test_send_request_survives_restart_and_rejects_changed_payload(tmp_path):
    path = str(tmp_path / 'send.db')
    payload = {'text': 'hello', 'account': ACCOUNT, 'chat_id': CHAT}
    ledger = SendLedger(path)
    assert ledger.reserve('request01', payload) is None
    with pytest.raises(ValueError, match='未知'):
        SendLedger(path).reserve('request01', payload)
    result = {'ok': True, 'sent': True, 'request_id': 'request01'}
    ledger.submitted('request01', result)
    assert SendLedger(path).reserve('request01', payload) == result
    with pytest.raises(ValueError):
        ledger.reserve('request01', dict(payload, chat_id='other'))


def test_original_parts_enter_both_stages_without_send(tmp_path, monkeypatch):
    monkeypatch.setenv('MAIBOT_ASSET_SPOOL', str(tmp_path / 'assets'))
    bridge = WeChatLiveBridge(api_key='offline-test', db_path=str(tmp_path / 'truth.db'),
                              target_chat_id=CHAT, base_url='https://example.invalid')
    bridge.bound_account = ACCOUNT
    bridge.conversation_key = KEY
    row = bridge.assets.ingest(DATA, ITEM, source_id='fixture')
    bridge.assets.bind_conversation('fixture', ACCOUNT, CHAT, 'test exact-path fixture')
    from src.platform_io.wechat.message_ingestor import MessageLedger
    from src.companion_vault.message_media_worker import MessageMediaQueue
    ledger=MessageLedger(str(tmp_path/'message_ledger.db'))
    ledger.ingest(bridge.edge_url,'epoch',dict(seq=1,account=ACCOUNT,msg_id='1',chat_id=CHAT,revision='r1',observed_at_ms=1,
        row=dict(msgId='1',talker=CHAT,type='3',content='',isSend='0')))
    MessageMediaQueue(bridge.assets,ledger.db,bridge.edge_url,None)
    with bridge.db.conn:
        bridge.db.conn.execute("INSERT INTO message_media_jobs(job_id,source,account,msg_id,revision,chat_id,ref_index,reference,state,updated_at) VALUES('fixture',?,?,?,?,?,0,'{}','ready',0)",(bridge.edge_url,ACCOUNT,'1','r1',CHAT))
        bridge.db.conn.execute("INSERT INTO message_asset_links VALUES('fixture',?,'original',?,?,?,?,1,'{}')",(row['file_code'],ACCOUNT,'1','r1',CHAT))
    ledger.db.close()
    monkeypatch.setattr(bridge, '_capture_current_media', lambda bubbles: ([row['file_code']], 'verified'))
    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            text = json.dumps(dict(inner_thought='test', should_reply=True, memory_greenlights=[],
                                  person_greenlights=[], file_greenlights=[], reply_direction='test', reply_style='简短表达'))
        else:
            text = '["收到"]'
        return SimpleNamespace(text=text)
    real_client = bridge.client
    bridge.client = SimpleNamespace(models=SimpleNamespace(generate_content=generate), close=lambda: None)
    monkeypatch.setattr(bridge, '_post', lambda *args: pytest.fail('NOOP must never POST'))
    state = WeChatScreenState('group', True, (BUBBLE,), (300, 2200), None, KEY)
    try:
        result = bridge.process_incoming_bubbles([BUBBLE], state)
        assert result['sent'] == [] and result['segments'] == ['收到']
        assert len(calls) == 2
        for call in calls:
            native = [part for part in call['contents'] if not isinstance(part, str)]
            assert native[0].inline_data.data == DATA
    finally:
        bridge.close()
        real_client.close()


def test_old_self_bubble_cannot_confirm_new_send(tmp_path, monkeypatch):
    monkeypatch.setenv('MAIBOT_ASSET_SPOOL', str(tmp_path / 'assets'))
    bridge = WeChatLiveBridge(api_key='offline', db_path=str(tmp_path / 'truth.db'), target_chat_id=CHAT,
                              send_enabled=True, base_url='https://example.invalid')
    bridge.bound_account, bridge.conversation_key = ACCOUNT, KEY
    old = WeChatBubble('SELF', '我', 'text', 'hello', (600, 300, 900, 400), (750, 350), 'old-self')
    state = WeChatScreenState('group', True, (old,), (300, 1300), None, KEY)
    monkeypatch.setattr(bridge, 'fetch_screen_state', lambda: ('xml', state))
    calls = []
    monkeypatch.setattr(bridge, '_post', lambda path, payload: calls.append(payload) or {'ok': True, 'sent': True})
    monkeypatch.setattr('src.mobile_agent.wechat_live_bridge.time.sleep', lambda _: None)
    try:
        with pytest.raises(RuntimeError, match='未确认'):
            bridge.send_wechat_bubbles(['hello'], state, reply_to=['incoming1'])
        assert len(calls) == 1
        assert calls[0]['expected_account'] == ACCOUNT
        assert 'send_x' not in calls[0] and len(calls[0]['request_id']) == 64
    finally:
        bridge.close()
