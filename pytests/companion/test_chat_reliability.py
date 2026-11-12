import sqlite3

import pytest

from src.platform_io.wechat.screen_fallback import WeChatScreenParser
from src.platform_io.wechat.wechat_text import format_for_wechat
from src.companion_vault.truth_db import CompanionTruthDB


def screen(messages, title="same title", offset=0):
    nodes = [f'<node text="{title}" bounds="[300,100][700,180]"/>',
             '<node class="android.widget.EditText" bounds="[200,2000][800,2100]"/>']
    for index, content in enumerate(messages):
        y = 300 + index * 160 + offset
        nodes.extend([f'<node content-desc="alice头像" bounds="[10,{y}][110,{y+100}]"/>',
                      f'<node text="{content}" bounds="[140,{y+40}][500,{y+100}]" clickable="true"/>'])
    return '<hierarchy>' + ''.join(nodes) + '</hierarchy>'


@pytest.mark.parametrize('content', ['same', ''])
def test_repeated_messages_are_distinct_and_repoll_is_not_new(content):
    parser = WeChatScreenParser()
    for count in range(1, 5):
        state = parser.parse_screen(screen([content] * count), conversation_id='account/chat')
        assert len(parser.extract_new_incoming_bubbles(state)) == 1
        assert len({b.fingerprint for b in state.bubbles}) == count
        state = parser.parse_screen(screen([content] * count), conversation_id='account/chat')
        assert parser.extract_new_incoming_bubbles(state) == []


def test_scroll_resize_rename_and_same_named_groups():
    parser = WeChatScreenParser()
    initial = parser.parse_screen(screen(['a', 'b', 'c']), conversation_id='account/chat')
    parser.extract_new_incoming_bubbles(initial)
    shifted = parser.parse_screen(screen(['b', 'c'], title='renamed', offset=20), conversation_id='account/chat')
    assert parser.extract_new_incoming_bubbles(shifted) == []
    other = parser.parse_screen(screen(['b', 'c'], title='renamed'), conversation_id='account/other')
    assert len(parser.extract_new_incoming_bubbles(other)) == 1


def test_sql_failed_batch_rolls_back_and_denies_control_and_foreign_tables():
    db = CompanionTruthDB(':memory:')
    db.conn.execute('CREATE TABLE secret(value TEXT)')
    for sql in ['BEGIN;', 'PRAGMA writable_schema=ON;', 'SELECT 1;', 'ATTACH DATABASE ":memory:" AS other;',
                'CREATE TRIGGER bad AFTER INSERT ON user_status BEGIN DELETE FROM user_status; END;',
                "INSERT INTO user_status(key) SELECT value FROM secret;",
                "INSERT INTO user_status(key) VALUES('x'); INSERT INTO user_status(key) VALUES('x');"]:
        with pytest.raises((sqlite3.DatabaseError, ValueError)):
            db.execute_table_sql(sql)
        assert db.conn.execute('SELECT count(*) FROM user_status').fetchone()[0] == 0
        assert not db.conn.in_transaction
    db.execute_table_sql("/* comment ; */ INSERT INTO user_status(key,current_value) VALUES('x','a;b'); -- end")
    assert db.conn.execute('SELECT current_value FROM user_status').fetchone()[0] == 'a;b'
    db.close()


def test_formatter_keeps_message_and_removes_private_thought():
    assert format_for_wechat('<think>secret</think>**你好**') == '你好'


def test_voice_duration_and_play_icon_are_one_message():
    xml = '<hierarchy><node text="group" bounds="[300,100][700,180]"/>' \
          '<node content-desc="alice头像" bounds="[10,300][110,420]"/>' \
          '<node content-desc="语音4秒未播放" bounds="[140,310][420,420]"/>' \
          '<node text="4&quot;" bounds="[220,320][300,410]"/>' \
          '<node class="android.widget.ImageView" clickable="true" bounds="[490,310][630,420]"/>' \
          '<node class="android.widget.EditText" bounds="[200,2000][800,2100]"/></hierarchy>'
    state = WeChatScreenParser().parse_screen(xml,conversation_id='account/group')
    assert [(b.msg_type,b.content) for b in state.bubbles] == [('voice','语音4秒未播放')]


def test_sql_cannot_grant_owner_or_replace_identity():
    db = CompanionTruthDB(':memory:')
    with db.conn:
        db.conn.execute("INSERT INTO person_info(person_id,person_name,trust_role,platform_ids) VALUES('owner','paff','OWNER','{}')")
        db.conn.execute("INSERT INTO chronicle_events(code_index,summary,chronicle_text) VALUES('AM0001','s','secret')")
    for sql in ["UPDATE person_info SET trust_role='OWNER' WHERE person_id='member'; INSERT INTO person_info(person_id,trust_role) VALUES('attacker','OWNER');",
                "DELETE FROM person_info WHERE person_id='owner';",
                "UPDATE person_info SET platform_ids='{\"wechat\":\"attacker\"}' WHERE person_id='owner';",
                "UPDATE person_info SET person_name='许妍' WHERE person_id='owner';",
                "UPDATE person_info SET impression=(SELECT chronicle_text FROM chronicle_events WHERE code_index='AM0001') WHERE person_id='owner';"]:
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            db.execute_table_sql(sql)
    assert db.conn.execute('SELECT count(*) FROM person_info').fetchone()[0] == 1
    assert db.conn.execute("SELECT person_name FROM person_info WHERE person_id='owner'").fetchone()[0] == 'paff'
    with db.conn:
        db.conn.execute("UPDATE person_info SET wechat_remark='paff_remark' WHERE person_id='owner'")
    with pytest.raises(ValueError, match="wechat_remark"):
        db.execute_table_sql("UPDATE person_info SET wechat_remark='tampered' WHERE person_id='owner'")
    assert db.conn.execute("SELECT wechat_remark FROM person_info WHERE person_id='owner'").fetchone()[0] == 'paff_remark'
    db.execute_table_sql("UPDATE person_info SET impression='hello; world' WHERE person_id='owner'")
    db.close()
