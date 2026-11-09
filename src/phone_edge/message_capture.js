'use strict';
// Pinned to verified WCDB database API; reads source messages, writes only our journal, and executes whitelisted outbound sends via ISendMsgMgr.
const counters = {ready: false, inserts: 0, sweeps: 0, errors: 0, sends: 0, send_errors: 0};
let source = null, account = null, queue = null, busy = false, dirty = true, tickCount = 0, lastGroupScanMs = 0;
const sources = new Map();
const pendingIds = new Map();
const activeSends = new Map();
const claimedMsgIds = new Set();
const imgDownloadAttempts = new Map();
const pendingImgDownloads = new Map();
const sendWindowMs = [];
const SPOOL_PATH = '/data/user/0/com.tencent.mm/files/maibot_send_spool';
let AndroidDB, Values, Digest, JString, JFile, FileInputStream, Scanner, ALog, Throwable;
const columns = ['msgId','msgSvrId','type','status','isSend','createTime','talker','content',
 'imgPath','reserved','transContent','talkerId','bizClientMsgId','bizChatId','bizChatUserId','msgSeq','flag','historyId'];
function hash(s) {
 const bytes = Digest.getInstance('SHA-256').digest(JString.$new(s).getBytes('UTF-8'));
 return Array.from(bytes).map(x => ('0'+((x+256)%256).toString(16)).slice(-2)).join('');
}
function query(db, sql) {
 if (db === queue) return queue.rawQuery.overload('java.lang.String','[Ljava.lang.String;').call(queue,sql,null);
 return source.rawQuery.overload('java.lang.String','[Ljava.lang.Object;').call(source,sql,null);
}
function scalar(sql) {
 const c=query(queue,sql);try{return c.moveToFirst()?String(c.getString(0)):null;}finally{c.close();}
}
function initialize(db) {
 const path=db.getPath().toString();const match=path.match(/\/MicroMsg\/([0-9a-f]{32})\/EnMicroMsg\.db$/);
 if(!match || !db.isOpen())return;
 if(sources.has(match[1]))return;
 source=Java.retain(db);account=match[1];
 sources.set(account,source);
 if(!queue)queue=AndroidDB.openOrCreateDatabase.overload('java.lang.String','android.database.sqlite.SQLiteDatabase$CursorFactory')
  .call(AndroidDB,'/data/user/0/com.tencent.mm/files/maibot_message_journal.db',null);
 queue.enableWriteAheadLogging();queue.execSQL('PRAGMA synchronous=FULL');
 queue.execSQL('CREATE TABLE IF NOT EXISTS journal_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)');
 queue.execSQL("INSERT OR IGNORE INTO journal_meta VALUES('epoch','"+Java.use('java.util.UUID').randomUUID().toString()+"')");
 queue.execSQL('CREATE TABLE IF NOT EXISTS capture_state(account TEXT PRIMARY KEY, highwater INTEGER NOT NULL DEFAULT 0,sweep INTEGER NOT NULL DEFAULT 0)');
 queue.execSQL("INSERT OR IGNORE INTO capture_state(account) VALUES('"+account+"')");
 queue.execSQL('CREATE TABLE IF NOT EXISTS message_heads(account TEXT,msg_id TEXT,revision TEXT NOT NULL,PRIMARY KEY(account,msg_id))');
 queue.execSQL('CREATE TABLE IF NOT EXISTS message_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT NOT NULL,msg_id TEXT NOT NULL,revision TEXT NOT NULL,payload TEXT NOT NULL)');
 queue.execSQL('CREATE INDEX IF NOT EXISTS message_event_identity ON message_events(account,msg_id,revision,seq)');
 queue.execSQL('CREATE TABLE IF NOT EXISTS send_outbox(request_id TEXT PRIMARY KEY,account TEXT NOT NULL,chat_id TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,msg_id TEXT,msg_svr_id TEXT,create_time TEXT,send_layer TEXT,chatting_context_talker TEXT,error TEXT)');
 try{
  const spool=JFile.$new(SPOOL_PATH);if(!spool.exists())spool.mkdirs();
  spool.setReadable(true,false);spool.setWritable(true,false);spool.setExecutable(true,false);
 }catch(e){}
 try{
  const rc=query(queue,"SELECT request_id,account,chat_id,content,created_at_ms,send_layer,chatting_context_talker FROM send_outbox WHERE status='sending' AND account='"+account+"' AND created_at_ms>"+(Date.now()-60000));
  try{while(rc.moveToNext()){
   const rId=String(rc.getString(0)||'');
   if(rId && !activeSends.has(rId)){
    const createdMs=Number(rc.getString(4)||Date.now());
    activeSends.set(rId,{
     request_id:rId,account:String(rc.getString(1)||account),chat_id:String(rc.getString(2)||''),
     content:String(rc.getString(3)||''),created_at_ms:createdMs,updated_at_ms:Date.now(),
     maxMsgIdBefore:'0',startedAtMs:createdMs,
     send_layer:rc.isNull(5)?'recovered':String(rc.getString(5)),
     chatting_context_talker:rc.isNull(6)?'none':String(rc.getString(6)),matchedMsgId:null
    });
   }
  }}finally{rc.close();}
 }catch(e){}
 try{scanGroupMembers();lastGroupScanMs=Date.now();}catch(e){}
 counters.ready=true;console.log(JSON.stringify({event:'capture_ready',account:account}));
}
function putOutbox(entry) {
 if(!queue)return;
 const v=Values.$new();const put=v.put.overload('java.lang.String','java.lang.String');
 put.call(v,'request_id',String(entry.request_id));
 put.call(v,'account',String(entry.account||account||''));
 put.call(v,'chat_id',String(entry.chat_id||''));
 put.call(v,'content',String(entry.content||''));
 put.call(v,'status',String(entry.status));
 put.call(v,'created_at_ms',String(entry.created_at_ms||Date.now()));
 put.call(v,'updated_at_ms',String(entry.updated_at_ms||Date.now()));
 if(entry.msg_id)put.call(v,'msg_id',String(entry.msg_id));
 if(entry.msg_svr_id)put.call(v,'msg_svr_id',String(entry.msg_svr_id));
 if(entry.create_time)put.call(v,'create_time',String(entry.create_time));
 if(entry.send_layer)put.call(v,'send_layer',String(entry.send_layer));
 if(entry.chatting_context_talker!==undefined && entry.chatting_context_talker!==null)
  put.call(v,'chatting_context_talker',String(entry.chatting_context_talker));
 if(entry.error)put.call(v,'error',String(entry.error));
 queue.insertWithOnConflict('send_outbox',null,v,5);v.$dispose();
}
function readFileUtf8(file) {
 const fis=FileInputStream.$new(file);
 try{const sc=Scanner.$new(fis,'UTF-8').useDelimiter('\\A');return sc.hasNext()?String(sc.next()):'';}
 finally{fis.close();}
}
function inspectChattingContext() {
 let state='none';
 try{
  Java.choose('com.tencent.mm.ui.chatting.component.an',{
   onMatch:function(inst){
    try{
     const resumed=Boolean(inst.g.value);const ctx=inst.d.value;
     const talker=ctx?String(ctx.x.overload().call(ctx)||''):'';
     if(talker)state=(resumed?'active:':'paused:')+talker;
    }catch(e){}
   },onComplete:function(){}
  });
 }catch(e){}
 return state;
}
function escapeXml(s){
 if(!s)return '';
 return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&apos;');
}
function sendPlainText(chatId, content, maxMsgIdBefore, baseEntry, reqId, chattingState, fallbackLayer) {
 const now=Date.now();
 const msgType=Number(Java.use('l41.d2').C(chatId))||1;
 const usePpc=Boolean(Java.use('f61.u1').a(chatId));
 const rawMgr=Java.use('no3.w1').a();
 const sendMgr=Java.cast(rawMgr,Java.use('rt5.i5'));
 const layer=fallbackLayer||('ISendMsgMgr:rt5.i5.Jj(via no3.w1.a,type='+msgType+',ppc='+usePpc+')');
 putOutbox(Object.assign({},baseEntry,{status:'sending',send_layer:layer,chatting_context_talker:chattingState}));
 sendWindowMs.push(now);
 sendMgr.Jj.overload('java.lang.String','java.lang.String','int','int').call(sendMgr,chatId,content,msgType,0);
 counters.sends++;
 activeSends.set(reqId,Object.assign({},baseEntry,{
  maxMsgIdBefore:maxMsgIdBefore,
  startedAtMs:now,
  send_layer:layer,
  chatting_context_talker:chattingState,
  matchedMsgId:null,
  isQuote:false
 }));
 console.log(JSON.stringify({event:'send_invoked',request_id:reqId,chat_id:chatId,max_msg_id_before:maxMsgIdBefore,send_layer:layer,chatting_context_talker:chattingState}));
}
function processSendSpool() {
 if(!queue || !source || !source.isOpen())return;
 const dir=JFile.$new(SPOOL_PATH);if(!dir.exists())return;
 const files=dir.listFiles();if(!files || !files.length)return;
 for(let i=0;i<files.length;i++){
  const f=files[i];const name=String(f.getName());
  if(!name.endsWith('.json') || name.startsWith('.'))continue;
  let raw='';
  try{raw=readFileUtf8(f);}catch(e){continue;}
  if(!raw){try{f.delete();}catch(e){}continue;}
  let req=null;
  try{req=JSON.parse(raw);}catch(e){try{f.delete();}catch(e2){}continue;}
  const reqId=String(req.request_id||'');
  if(!/^[0-9a-zA-Z_-]{8,128}$/.test(reqId)){try{f.delete();}catch(e){}continue;}
  const existing=scalar("SELECT status FROM send_outbox WHERE request_id='"+reqId+"'");
  if(existing!==null && existing!=='accepted'){try{f.delete();}catch(e){}continue;}
  const now=Date.now();
  const chatId=String(req.chat_id||'');const content=String(req.content||'');
  const reqAccount=String(req.account||'');
  const baseEntry={request_id:reqId,account:reqAccount||account,chat_id:chatId,content:content,created_at_ms:now,updated_at_ms:now};
  if(existing===null){
   putOutbox(Object.assign({},baseEntry,{status:'accepted'}));
  }
  try{f.delete();}catch(e){}
  if(reqAccount!==account || !/^[0-9a-zA-Z_@-]{3,64}$/.test(chatId) || !content.trim()){
   putOutbox(Object.assign({},baseEntry,{status:'failed',error:'Invalid account, chat_id, or empty content'}));
   continue;
  }
  const allowed=Array.isArray(req.allowed_chats)?req.allowed_chats:['*'];
  if(allowed.length && !allowed.includes('*') && !allowed.includes(chatId)){
   putOutbox(Object.assign({},baseEntry,{status:'failed',error:'Target chat_id not in Frida allowlist'}));
   continue;
  }
  const maxPerMin=Math.max(1,Number(req.max_per_minute)||15);
  while(sendWindowMs.length && sendWindowMs[0]<=now-60000)sendWindowMs.shift();
  const dbRecent=Number(scalar("SELECT count(*) FROM send_outbox WHERE status IN ('sending','confirmed','submitted_unconfirmed') AND created_at_ms>"+(now-60000))||'0');
  const usedCount=Math.max(sendWindowMs.length,dbRecent);
  if(usedCount>=maxPerMin){
   putOutbox(Object.assign({},baseEntry,{status:'rate_limited',error:'In-process per-minute rate limit exceeded ('+usedCount+'/'+maxPerMin+')'}));
   console.log(JSON.stringify({event:'send_rate_limited',request_id:reqId,chat_id:chatId,used:usedCount,limit:maxPerMin}));
   continue;
  }
  let maxMsgIdBefore='0';
  const mc=query(source,'SELECT coalesce(max(msgId),0) FROM message');
  try{if(mc.moveToFirst())maxMsgIdBefore=String(mc.getString(0)||'0');}finally{mc.close();}
  const chattingState=inspectChattingContext();
  try{
   const isQuote=Boolean(req.msg_type==='quote' && req.quote && req.quote.server_id);
   if(isQuote){
    const q=req.quote;
    const targetSvrId=String(q.server_id||'');
    let targetExists=false;
    try{
     const tc=query(source,"SELECT msgId FROM message WHERE msgSvrId='"+targetSvrId+"' LIMIT 1");
     if(tc.moveToFirst())targetExists=true;
     tc.close();
    }catch(e){}

    if(!targetExists){
     console.log(JSON.stringify({event:'send_quote_target_not_found_fallback',request_id:reqId,chat_id:chatId,target_svrid:targetSvrId}));
     sendPlainText(chatId,content,maxMsgIdBefore,baseEntry,reqId,chattingState,'fallback_text_from_quote');
     continue;
    }

    try{
     const cSec=q.createtime?Number(q.createtime):Math.floor(now/1000);
     const isChatRoom=chatId.endsWith('@chatroom');
     const fromUsr=isChatRoom ? (q.talker||chatId) : (q.sender_id||chatId);
     const chatUsr=isChatRoom ? (q.sender_id||'') : '';
     let selfWxid = '';
     try {
       const uic = query(source, 'SELECT value FROM userinfo WHERE id=2');
       if (uic.moveToFirst()) selfWxid = String(uic.getString(0) || '');
       uic.close();
     } catch(e) {}
     if (!selfWxid) selfWxid = account;
     const xml='<?xml version="1.0"?>\n<msg>\n\t<appmsg appid="" sdkver="0">\n\t\t<title>'
       +escapeXml(content)+'</title>\n\t\t<action>view</action>\n\t\t<type>57</type>\n\t\t<refermsg>\n\t\t\t<type>1</type>\n\t\t\t<svrid>'
       +escapeXml(String(q.server_id))+'</svrid>\n\t\t\t<fromusr>'
       +escapeXml(String(fromUsr))+'</fromusr>\n\t\t\t<chatusr>'
       +escapeXml(String(chatUsr))+'</chatusr>\n\t\t\t<displayname>'
       +escapeXml(String(q.display_name||q.sender_id||''))+'</displayname>\n\t\t\t<content>'
       +escapeXml(String(q.content||''))+'</content>\n\t\t\t<createtime>'
       +cSec+'</createtime>\n\t\t</refermsg>\n\t\t<appattach />\n\t</appmsg>\n\t<fromusername>'
       +escapeXml(selfWxid)+'</fromusername>\n\t<scene>0</scene>\n\t<appinfo><version>1</version><appname /></appinfo>\n\t<commenturl />\n</msg>';
     const appMsg=Java.use('nx0.r').v(xml);
     if(!appMsg)throw new Error('Failed to parse quote AppMsg XML');
     const layer='AppMsgLogic:k0.I(type=57,svrid='+q.server_id+')';
     putOutbox(Object.assign({},baseEntry,{status:'sending',send_layer:layer,chatting_context_talker:chattingState}));
     sendWindowMs.push(now);
     const pair=Java.use('com.tencent.mm.pluginsdk.model.app.k0').I(appMsg,"","",chatId,"",null);
     const rawSecond=pair?pair.second:null;
     const localId=(rawSecond!==null && rawSecond!==undefined)?String(rawSecond.value!==undefined?rawSecond.value:rawSecond):null;
     const safeLocalId=(localId && localId!=='null' && localId!=='undefined')?localId:null;
     counters.sends++;
     activeSends.set(reqId,Object.assign({},baseEntry,{
       maxMsgIdBefore:maxMsgIdBefore,
       startedAtMs:now,
       send_layer:layer,
       chatting_context_talker:chattingState,
       matchedMsgId:safeLocalId,
       isQuote:true,
       quoted_svr_id:String(q.server_id||'')
     }));
     console.log(JSON.stringify({event:'send_quote_invoked',request_id:reqId,chat_id:chatId,max_msg_id_before:maxMsgIdBefore,local_id:safeLocalId,send_layer:layer,chatting_context_talker:chattingState}));
    }catch(quoteErr){
     console.log(JSON.stringify({event:'send_quote_failed_fallback_plain',request_id:reqId,error:String(quoteErr)}));
     sendPlainText(chatId,content,maxMsgIdBefore,baseEntry,reqId,chattingState,'fallback_text_from_quote');
    }
   }else{
    sendPlainText(chatId,content,maxMsgIdBefore,baseEntry,reqId,chattingState,null);
   }
  }catch(e){
   counters.send_errors++;
   putOutbox(Object.assign({},baseEntry,{status:'failed',chatting_context_talker:chattingState,error:String(e)}));
   console.log(JSON.stringify({event:'send_invoke_error',request_id:reqId,error:String(e)}));
  }
 }
}
function pollActiveSends() {
 if(!activeSends.size || !source || !source.isOpen())return;
 for(const entry of Array.from(activeSends.entries())){
  const reqId=entry[0];const item=entry[1];const now=Date.now();
  const c=query(source,"SELECT msgId,msgSvrId,status,createTime,content,type FROM message WHERE msgId>"+item.maxMsgIdBefore+" AND isSend=1 AND talker='"+item.chat_id+"' ORDER BY msgId ASC LIMIT 10");
  let matched=null;
  try{while(c.moveToNext()){
   const row={
    msgId:String(c.getString(0)||''),
    msgSvrId:c.isNull(1)?'0':String(c.getString(1)),
    status:String(c.getString(2)||''),
    createTime:String(c.getString(3)||''),
    content:c.isNull(4)?'':String(c.getString(4)),
    type:c.isNull(5)?0:Number(c.getString(5)||'0')
   };
   const rowCreateTime=Number(row.createTime)||0;
   const timeThreshold=(item.startedAtMs||item.created_at_ms||now)-2000;
   const isTypeMatch=(row.type===822083633 || row.type===49 || row.type===1);
   const normRowContent=(row.content||'').replace(/&#x20;/g,' ').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"');
   const isContentMatch=(row.content===item.content) || (item.content && normRowContent.indexOf(item.content)!==-1);
   const isQuoteSvrMatch=Boolean(item.isQuote && item.quoted_svr_id && row.content.indexOf('<svrid>'+item.quoted_svr_id+'</svrid>')!==-1);
   if(item.matchedMsgId===row.msgId || (!item.matchedMsgId && !claimedMsgIds.has(row.msgId) && isTypeMatch && rowCreateTime>=timeThreshold && (isContentMatch || isQuoteSvrMatch))){
    matched=row;break;
   }
  }}finally{c.close();}
  if(matched){
   if(!item.matchedMsgId){item.matchedMsgId=matched.msgId;claimedMsgIds.add(matched.msgId);}
   if(!pendingIds.has(account))pendingIds.set(account,new Set());
   pendingIds.get(account).add(matched.msgId);dirty=true;
   if(matched.status==='2' && matched.msgSvrId && matched.msgSvrId!=='0' && matched.msgSvrId!=='null'){
    try{scan(false,false,[matched.msgId]);pendingIds.get(account).delete(matched.msgId);}catch(e){}
    putOutbox(Object.assign({},item,{status:'confirmed',updated_at_ms:now,msg_id:matched.msgId,msg_svr_id:matched.msgSvrId,create_time:matched.createTime}));
    console.log(JSON.stringify({event:'send_confirmed',request_id:reqId,msg_id:matched.msgId,msg_svr_id:matched.msgSvrId,create_time:matched.createTime,send_layer:item.send_layer,chatting_context_talker:item.chatting_context_talker}));
    activeSends.delete(reqId);
   }else if(matched.status==='5'){
    putOutbox(Object.assign({},item,{status:'failed',updated_at_ms:now,msg_id:matched.msgId,msg_svr_id:matched.msgSvrId,create_time:matched.createTime,error:'WeChat message row status=5'}));
    activeSends.delete(reqId);
   }else if(now-item.startedAtMs>15000){
    putOutbox(Object.assign({},item,{status:'submitted_unconfirmed',updated_at_ms:now,msg_id:matched.msgId,msg_svr_id:matched.msgSvrId,create_time:matched.createTime,error:'Timed out waiting for status=2 server ack'}));
    activeSends.delete(reqId);
   }
  }else if(now-item.startedAtMs>15000){
   putOutbox(Object.assign({},item,{status:'submitted_unconfirmed',updated_at_ms:now,error:'Timed out waiting for local DB message row'}));
   activeSends.delete(reqId);
  }
 }
}
function scan(sweep, tail, ids) {
 // Queries run on the agent thread, never on a transaction's writer thread.
 if(!source.isOpen()){sources.delete(account);source.$dispose();source=null;counters.ready=false;return;}
 if(source.inTransaction())throw new Error('Agent thread unexpectedly owns a source transaction');
 const key=sweep?'sweep':'highwater';const cursor=scalar("SELECT "+key+" FROM capture_state WHERE account='"+account+"'")||'0';
 if(!/^\d+$/.test(cursor))throw new Error('Invalid persisted cursor');
 const clause=ids?'WHERE msgId IN ('+ids.join(',')+') ORDER BY msgId':tail?'ORDER BY msgId DESC LIMIT 100':'WHERE msgId>'+cursor+' ORDER BY msgId LIMIT 200';
 const c=query(source,'SELECT '+columns.join(',')+' FROM message '+clause);
 const rows=[];try{while(c.moveToNext()){
  const row={};columns.forEach((name,i)=>{row[name]=c.isNull(i)?null:String(c.getString(i));});rows.push(row);
 }}finally{c.close();}
 for(const row of rows){
  const kind=Number(row.type)&65535;
  if(kind===47){
   let md5=row.imgPath||'';
   if(!md5 || md5.length!==32){
    const m=row.content?row.content.match(/md5="([a-f0-9]{32})"/):null;
    if(m)md5=m[1];
   }
   if(md5 && md5.length===32){
    try{
     const EmojiInfo=Java.use('com.tencent.mm.storage.emotion.EmojiInfo');
     const emoji=EmojiInfo.$new();
     emoji.field_md5.value=md5;
     emoji.X();
    }catch(e){}
   }
  }
 }
 const byId=new Map(rows.map(row=>[row.msgId,row]));
 const byServer=new Map(rows.filter(row=>row.msgSvrId && /^\d+$/.test(row.msgSvrId) && row.msgSvrId!=='0').map(row=>[row.msgSvrId,row]));
 const mediaSpecs=[
  {types:[3],table:'ImgInfo2',id:'msglocalid',columns:['id','msglocalid','msgSvrId','bigImgPath','midImgPath','hevcPath','thumbImgPath','offset','totalLen','iscomplete','origImgMD5','msgTalker']},
  {types:[34],table:'voiceinfo',id:'MsgLocalId',columns:['MsgLocalId','FileName','TotalLen','FileNowSize','VoiceLength','Status','MsgTalker']},
  {types:[43],table:'videoinfo2',id:'msglocalid',columns:['msglocalid','filename','video_path','totallen','filenowsize','videomd5','status','msg_talker']},
  {types:[49],table:'appattach',id:'msgInfoId',columns:['msgInfoId','fileFullPath','totalLen','offset','status','msgInfoTalker']}
 ];
 for(const spec of mediaSpecs){
  const ids=rows.filter(row=>spec.types.includes(Number(row.type)&65535)).map(row=>row.msgId);
  if(!ids.length)continue;
  const servers=spec.table==='ImgInfo2'?rows.filter(row=>(Number(row.type)&65535)===3 && byServer.has(row.msgSvrId)).map(row=>row.msgSvrId):[];
  const condition=spec.id+' IN ('+ids.join(',')+')'+(servers.length?' OR msgSvrId IN ('+servers.join(',')+')':'');
  const cursor=query(source,'SELECT '+spec.columns.join(',')+' FROM '+spec.table+' WHERE '+condition+' ORDER BY rowid');
  try{while(cursor.moveToNext()){
   const info={table:spec.table};spec.columns.forEach((name,i)=>{info[name]=cursor.isNull(i)?null:String(cursor.getString(i));});
   const row=byId.get(info[spec.id])||(spec.table==='ImgInfo2'?byServer.get(info.msgSvrId):null);
   if(row){if(!row._media_info)row._media_info=[];row._media_info.push(info);}
  }}finally{cursor.close();}
 }
 for(const row of rows){
  const kind=Number(row.type)&65535;
  if(kind===3 && Array.isArray(row._media_info)){
   let hasLocalFull=false;
   let targetInfo=null;
   for(const info of row._media_info){
    if(info.table!=='ImgInfo2')continue;
    const big=info.bigImgPath||'';const mid=info.midImgPath||'';const hevc=info.hevcPath||'';
    if((big && !big.startsWith('SERVERID://')) || (mid && !mid.startsWith('SERVERID://')) || (hevc && !hevc.startsWith('SERVERID://'))){
     hasLocalFull=true;
    }else if(info.id && /^\d+$/.test(info.id)){
     targetInfo=info;
    }
   }
   if(hasLocalFull){
    pendingImgDownloads.delete(row.msgId);
   }else if(!sweep && targetInfo && row.msgId && row.talker && Number(row.msgId)>=143000){
    const now=Date.now();
    const lastAttempt=imgDownloadAttempts.get(row.msgId)||0;
    if(now-lastAttempt>60000){
     imgDownloadAttempts.set(row.msgId,now);
     pendingImgDownloads.set(row.msgId,now);
     try{
      try{
       const h2=Java.use('com.tencent.mm.modelcdntran.s1').Kj();
       const uSet=h2._u?h2._u.value:h2.u.value;
       if(uSet)uSet.add('image_'+row.msgId);
      }catch(e2){}
      const rawSvc=Java.cast(Java.use('ni5.n0').c(Java.use('da0.e').class),Java.use('da0.e')).Bj();
      const downloadSvc=Java.cast(rawSvc,Java.use('v51.j'));
      const scene=Java.use('v51.t0').$new(int64(String(targetInfo.id)),int64(String(row.msgId)),String(row.talker),0,downloadSvc,0);
      const netCore=Java.use('pp0.i1').q();
      const s1=netCore._b?netCore._b.value:netCore.b.value;
      const started=s1.g(scene);
      console.log(JSON.stringify({event:'img_download_triggered',msg_id:row.msgId,img_id:targetInfo.id,started:Boolean(started)}));
     }catch(e){
      console.log(JSON.stringify({event:'img_download_trigger_error',msg_id:row.msgId,error:String(e)}));
     }
    }
   }
  }
 }
 if(tail)rows.reverse();
 queue.beginTransaction();try{
  for(const row of rows){
   if(!row.msgId || !/^\d+$/.test(row.msgId)||!row.talker)throw new Error('Message missing stable identity');
   const revision=hash(JSON.stringify(row));const id=row.msgId;
   const old=scalar("SELECT revision FROM message_heads WHERE account='"+account+"' AND msg_id='"+id+"'");
   if(old!==revision){
    const event={schema:1,account:account,chat_id:row.talker,msg_id:id,revision:revision,
     operation:old===null?'upsert':'update',observed_at_ms:Date.now(),source:'committed_wcdb_read',row:row};
    const v=Values.$new();v.put.overload('java.lang.String','java.lang.String').call(v,'account',account);
    v.put.overload('java.lang.String','java.lang.String').call(v,'msg_id',id);
    v.put.overload('java.lang.String','java.lang.String').call(v,'revision',revision);
    v.put.overload('java.lang.String','java.lang.String').call(v,'payload',JSON.stringify(event));
    queue.insertOrThrow('message_events',null,v);v.$dispose();
    queue.execSQL("INSERT OR REPLACE INTO message_heads VALUES('"+account+"','"+id+"','"+revision+"')");
    counters.inserts++;
   }
  }
  const next=rows.length?rows[rows.length-1].msgId:(sweep?'0':cursor);
  if(!tail && !ids)queue.execSQL("UPDATE capture_state SET "+key+'='+next+" WHERE account='"+account+"'");
  queue.execSQL("INSERT OR REPLACE INTO journal_meta VALUES('last_scan_ms','"+Date.now()+"')");
  if(sweep && !rows.length){
   const count=query(source,'SELECT count(*) FROM message');
   try{if(count.moveToFirst())queue.execSQL("INSERT OR REPLACE INTO journal_meta VALUES('source_count:"+account+"','"+count.getString(0)+"')");}
   finally{count.close();}
   queue.execSQL("INSERT OR REPLACE INTO journal_meta VALUES('sweep_complete_ms:"+account+"','"+Date.now()+"')");
  }
  queue.setTransactionSuccessful();
 }finally{queue.endTransaction();}
 if(sweep)counters.sweeps++;
}
function scanGroupMembers(){
 const contactMap=new Map();
 try{
  const rc=query(source,'SELECT username,nickname,conRemark FROM rcontact');
  try{while(rc.moveToNext()){
   const u=String(rc.getString(0)||'');if(!u)continue;
   const nick=rc.isNull(1)?'':String(rc.getString(1)||'');
   const remark=rc.isNull(2)?'':String(rc.getString(2)||'');
   if(nick || remark)contactMap.set(u,{nickname:nick,remark:remark});
  }}finally{rc.close();}
 }catch(e){}
 let c=null;let hasDisplay=true;
 try{c=query(source,'SELECT chatroomname,memberlist,memberCount,displayname FROM chatroom');}
 catch(e){hasDisplay=false;c=query(source,'SELECT chatroomname,memberlist,memberCount FROM chatroom');}
 const rows=[];
 try{while(c.moveToNext()){
  const group=String(c.getString(0)||'');const raw=String(c.getString(1)||'');
  const members=Array.from(new Set(raw.split(';').filter(Boolean))).sort();
  const expected=c.isNull(2)?null:Number(c.getString(2));
  const disp=(hasDisplay && !c.isNull(3))?String(c.getString(3)||''):'';
  if(group.endsWith('@chatroom') && (expected===null || expected===members.length)){
   const groupContacts={};
   if(contactMap.has(group) || disp){
    const gc=contactMap.get(group)||{nickname:'',remark:''};
    groupContacts[group]={nickname:gc.nickname||disp,remark:gc.remark};
   }
   for(let m=0;m<members.length;m++){
    const uid=members[m];
    if(contactMap.has(uid))groupContacts[uid]=contactMap.get(uid);
   }
   rows.push([group,members,groupContacts]);
  }
 }}finally{c.close();}
 const allContacts={};
 for(const kv of Array.from(contactMap.entries())){
  allContacts[kv[0]]=kv[1];
 }
 rows.push(['__contacts__',Object.keys(allContacts),allContacts]);
 queue.execSQL('CREATE TABLE IF NOT EXISTS group_member_snapshots(account TEXT,group_id TEXT,payload TEXT,PRIMARY KEY(account,group_id))');
 queue.beginTransaction();try{
  for(const entry of rows){
   const value=Values.$new();
   value.put.overload('java.lang.String','java.lang.String').call(value,'account',account);
   value.put.overload('java.lang.String','java.lang.String').call(value,'group_id',entry[0]);
   value.put.overload('java.lang.String','java.lang.String').call(value,'payload',JSON.stringify({account:account,group_id:entry[0],user_ids:entry[1],contacts:entry[2],complete:true}));
   queue.insertWithOnConflict('group_member_snapshots',null,value,5);value.$dispose();
  }
  queue.setTransactionSuccessful();
 }finally{queue.endTransaction();}
}
Java.perform(function(){
 AndroidDB=Java.use('android.database.sqlite.SQLiteDatabase');Values=Java.use('android.content.ContentValues');
 Digest=Java.use('java.security.MessageDigest');JString=Java.use('java.lang.String');
 JFile=Java.use('java.io.File');FileInputStream=Java.use('java.io.FileInputStream');
 Scanner=Java.use('java.util.Scanner');ALog=Java.use('android.util.Log');Throwable=Java.use('java.lang.Throwable');
 const DB=Java.use('com.tencent.wcdb.database.SQLiteDatabase');
 ['insertWithOnConflict','updateWithOnConflict','delete'].forEach(name=>{
  if(!DB[name])return;
  DB[name].overloads.forEach(o=>{o.implementation=function(){
   const args=Array.prototype.slice.call(arguments);const result=o.apply(this,args);
   if(['message','ImgInfo2','appattach','voiceinfo','videoinfo2'].includes(String(args[0]))) {
    dirty=true;
    try {
     const match=this.getPath().toString().match(/\/MicroMsg\/([0-9a-f]{32})\/EnMicroMsg\.db$/);
     let id=null;
     if(String(args[0])==='message' && name==='insertWithOnConflict' && Number(result)>0){
      id=String(result);
      try{
       const cv=args[2];
       if(cv && String(cv.getAsString('isSend'))==='1'){
        console.log(JSON.stringify({event:'outbound_wcdb_insert',msg_id:id,talker:String(cv.getAsString('talker')||''),create_time:String(cv.getAsString('createTime')||''),stack:String(ALog.getStackTraceString(Throwable.$new()))}));
       }
      }catch(e){}
     }
     if(String(args[0])==='message' && name==='updateWithOnConflict' && Number(result)>0 && /^\s*msgId\s*=\s*\?\s*$/i.test(String(args[2])) && args[3] && args[3].length===1)id=String(args[3][0]);
     if(match && id && /^\d+$/.test(id)){
      if(!pendingIds.has(match[1]))pendingIds.set(match[1],new Set());
      const ids=pendingIds.get(match[1]);if(ids.size<4096)ids.add(id);
     }
    }catch(e){counters.errors++;console.log(JSON.stringify({event:'hint_error',error:String(e)}));}
   }
   return result;
  };});
 });
 setInterval(function(){Java.perform(function(){
  if(busy)return;busy=true;
  try{processSendSpool();pollActiveSends();}
  catch(e){counters.send_errors++;console.log(JSON.stringify({event:'send_poll_error',error:String(e)}));}
  finally{busy=false;}
 });},300);
 setInterval(function(){Java.perform(function(){
  if(busy)return;busy=true;
  try{
   if(!sources.size || tickCount%15===0){Java.choose('com.tencent.wcdb.database.SQLiteDatabase',{
    onMatch:function(db){initialize(db);},onComplete:function(){}
   });}
   for(const entry of Array.from(sources.entries())){
    account=entry[0];source=entry[1];
    if(!source.isOpen()){sources.delete(account);source.$dispose();source=null;continue;}
    processSendSpool();pollActiveSends();
    if(pendingImgDownloads.size){
     if(!pendingIds.has(account))pendingIds.set(account,new Set());
     const pSet=pendingIds.get(account);const now=Date.now();
     for(const dl of Array.from(pendingImgDownloads.entries())){
      if(now-dl[1]>45000)pendingImgDownloads.delete(dl[0]);
      else pSet.add(dl[0]);
     }
    }
    const pending=pendingIds.get(account);
    if(pending && pending.size){const ids=Array.from(pending).slice(0,100);scan(false,false,ids);ids.forEach(id=>pending.delete(id));}
    if(dirty||tickCount%2===0){
     if(dirty && Date.now()-lastGroupScanMs>10000){try{scanGroupMembers();lastGroupScanMs=Date.now();}catch(e){}}
     scan(false,true);if(source)scan(false,false);
    }
    if(source && tickCount%3===0)scan(true,false);
    if(source && tickCount%30===0){try{scanGroupMembers();lastGroupScanMs=Date.now();}catch(e){console.log(JSON.stringify({event:'group_members_scan_failed'}));}}
   }
   dirty=false;
   tickCount++;if(tickCount%15===0){
   console.log(JSON.stringify({event:'capture_heartbeat',stats:counters}));
  }
  }catch(e){counters.errors++;console.log(JSON.stringify({event:'capture_error',error:String(e)}));}
  finally{busy=false;}
 });},2000);
});
