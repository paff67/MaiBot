"""Read-only projection probe; never invoke a model or send a phone message."""
from pathlib import Path
import argparse
import json
import sqlite3

from src.platform_io.wechat.projection import project_message


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',required=True)
    parser.add_argument('--account',required=True)
    parser.add_argument('--chat',required=True)
    parser.add_argument('--source',required=True)
    parser.add_argument('--msg-id',required=True)
    args=parser.parse_args()
    with sqlite3.connect(Path(args.ledger).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM messages WHERE source=? AND account=? AND chat_id=? AND msg_id=?',
                       (args.source,args.account,args.chat,args.msg_id)).fetchone()
    db.close()
    if row is None:raise ValueError('Message not found in requested source/account/chat')
    result=project_message(dict(row))
    # Report identity/shape only; never print full personal messages or attachment XML.
    print(json.dumps({'message_id':result['message_id'],'platform':result['platform'],
                      'account_id':result['account_id'],'scope':result['scope'],
                      'components':[c['type'] for c in result['raw_message']],
                      'model_called':False,'phone_sent':False}))


if __name__=='__main__':main()
