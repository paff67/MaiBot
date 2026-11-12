import io
import json

import pytest

from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.model_assets import build_original_parts
from src.companion_vault.truth_db import CompanionTruthDB
from src.phone_edge.chat_identity import extract_media_references
from src.phone_edge.send_ledger import SendLedger


ACCOUNT = 'a' * 32
CHAT = '123@chatroom'
KEY = ACCOUNT + '/' + CHAT
DATA = b'\xff\xd8\xfffixture-original'
ITEM = dict(account=ACCOUNT, kind='image', key='image/ab/photo.jpg', relative_path='ab/photo.jpg',
            scan_ns=150, ctime_ns=150, mtime_ns=10, size=len(DATA), mime_type='image/jpeg')


def test_emoji_original_is_recallable_with_exact_path_evidence(tmp_path):
    item = dict(ITEM, kind='emoji', key='emoji/decrypt/hash', relative_path='decrypt/hash',
                mime_type='application/octet-stream', representation='opaque_original')
    path = f'/data/data/com.tencent.mm/MicroMsg/{ACCOUNT}/emoji/decrypt/hash'
    assert extract_media_references('filePath=' + path, ACCOUNT) == [path]
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

