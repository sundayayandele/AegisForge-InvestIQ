import os, sqlite3, time, requests, smtplib, json
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler

SYMBOLS='NVDA IONQ TSM MSFT AVGO MU CRWD RKLB OKLO CEG ASML VST ROK SOFI ENPH'.split()
DB=os.getenv('DB_PATH','/data/investiq.db')
API_KEY=os.getenv('FINNHUB_API_KEY','')
app=FastAPI(title='AegisForge InvestIQ',version='0.1.0')
scheduler=BackgroundScheduler(timezone='UTC')
@contextmanager
def connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB)),exist_ok=True)
    db=sqlite3.connect(DB,timeout=30)
    db.row_factory=sqlite3.Row
    try:
        yield db
        db.commit()
    finally: db.close()
def init():
    with connection() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY,price REAL NOT NULL,previous_close REAL,market_timestamp INTEGER NOT NULL,fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS history(symbol TEXT NOT NULL,market_timestamp INTEGER NOT NULL,price REAL NOT NULL,fetched_at INTEGER NOT NULL,PRIMARY KEY(symbol,market_timestamp));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,above REAL,below REAL,last_trigger INTEGER DEFAULT 0, CHECK(above IS NOT NULL OR below IS NOT NULL));
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity REAL NOT NULL,price REAL NOT NULL,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS paper_account(id INTEGER PRIMARY KEY CHECK(id=1),cash REAL NOT NULL);
INSERT OR IGNORE INTO paper_account VALUES(1,1000);''')
def notify(message):
    token=os.getenv('TELEGRAM_BOT_TOKEN',''); chat=os.getenv('TELEGRAM_CHAT_ID','')
    if token and chat:
        try: requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':message},timeout=10).raise_for_status()
        except requests.RequestException as e: print('Telegram failed:',e)
    if all(os.getenv(k) for k in ('SMTP_HOST','SMTP_USER','SMTP_PASSWORD','ALERT_EMAIL')):
        try:
            msg=EmailMessage();msg['Subject']='InvestIQ alert';msg['From']=os.environ['SMTP_USER'];msg['To']=os.environ['ALERT_EMAIL'];msg.set_content(message)
            with smtplib.SMTP_SSL(os.environ['SMTP_HOST'],int(os.getenv('SMTP_PORT','465')),timeout=10) as server:
                server.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD']);server.send_message(msg)
        except Exception as e: print('Email failed:',type(e).__name__)
def ingest():
    if not API_KEY:
        print('No FINNHUB_API_KEY: skipping ingestion');return
    now=int(time.time())
    for symbol in SYMBOLS:
        try:
            r=requests.get('https://finnhub.io/api/v1/quote',params={'symbol':symbol,'token':API_KEY},timeout=12);r.raise_for_status();q=r.json()
            price=float(q.get('c') or 0);stamp=int(q.get('t') or 0)
            if price<=0 or stamp<=0 or stamp>now+300: continue
            with connection() as db:
                old=db.execute('SELECT market_timestamp FROM quotes WHERE symbol=?',(symbol,)).fetchone()
                if old and stamp<old['market_timestamp']:continue
                db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?)',(symbol,price,q.get('pc'),stamp,now))
                db.execute('INSERT OR IGNORE INTO history VALUES(?,?,?,?)',(symbol,stamp,price,now))
                for a in db.execute('SELECT * FROM alerts WHERE symbol=?',(symbol,)).fetchall():
                    hit=(a['above'] is not None and price>=a['above']) or (a['below'] is not None and price<=a['below'])
                    if hit and now-a['last_trigger']>=3600 and (not old or stamp>old['market_timestamp']):
                        db.execute('UPDATE alerts SET last_trigger=? WHERE id=?',(now,a['id']))
                        notify(f'{symbol} crossed your alert: ${price:.2f} (market timestamp {stamp})')
        except (requests.RequestException,ValueError,TypeError) as e:print('Ingestion error',symbol,str(e)[:120])
        time.sleep(1)
@app.on_event('startup')
def startup():
    init()
    if os.getenv('ENABLE_SCHEDULER','1')=='1':
        scheduler.add_job(ingest,'interval',hours=1,id='hourly',max_instances=1,coalesce=True,replace_existing=True)
        scheduler.start()
@app.on_event('shutdown')
def shutdown():
    if scheduler.running:scheduler.shutdown(wait=False)
@app.get('/api/quotes')
def quotes():
    with connection() as db: rows={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
    return [{'symbol':s,**rows.get(s,{'price':None,'previous_close':None,'market_timestamp':None,'fetched_at':None})} for s in SYMBOLS]
@app.get('/api/history/{symbol}')
def history(symbol:str):
    if symbol not in SYMBOLS:raise HTTPException(404,'Unknown symbol')
    with connection() as db:return [dict(r) for r in db.execute('SELECT * FROM history WHERE symbol=? ORDER BY market_timestamp DESC LIMIT 500',(symbol,))]
class Alert(BaseModel):
    symbol:str;above:float|None=None;below:float|None=None
@app.post('/api/alerts')
def add_alert(a:Alert):
    if a.symbol not in SYMBOLS or (a.above is None and a.below is None) or any(v is not None and v<=0 for v in (a.above,a.below)):raise HTTPException(400,'Invalid alert')
    with connection() as db:db.execute('INSERT INTO alerts(symbol,above,below) VALUES(?,?,?)',(a.symbol,a.above,a.below))
    return {'ok':True}
@app.get('/api/alerts')
def alerts():
    with connection() as db:return [dict(r) for r in db.execute('SELECT * FROM alerts')]
class Trade(BaseModel):
    symbol:str;side:str;quantity:float=Field(gt=0,le=100000)
@app.post('/api/paper/trade')
def paper_trade(t:Trade):
    if t.symbol not in SYMBOLS or t.side not in ('buy','sell'):raise HTTPException(400,'Invalid trade')
    with connection() as db:
        q=db.execute('SELECT * FROM quotes WHERE symbol=?',(t.symbol,)).fetchone()
        if not q or time.time()-q['market_timestamp']>86400:raise HTTPException(409,'Missing or stale quote; paper trading unavailable')
        price=q['price'];cash=db.execute('SELECT cash FROM paper_account WHERE id=1').fetchone()['cash']
        held=db.execute("SELECT COALESCE(SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END),0) AS qty FROM trades WHERE symbol=?",(t.symbol,)).fetchone()['qty']
        if t.side=='buy' and cash+1e-8<t.quantity*price:raise HTTPException(400,'Insufficient paper cash')
        if t.side=='sell' and held+1e-8<t.quantity:raise HTTPException(400,'Insufficient paper shares')
        cash+=(-1 if t.side=='buy' else 1)*t.quantity*price
        db.execute('UPDATE paper_account SET cash=? WHERE id=1',(cash,))
        db.execute('INSERT INTO trades(symbol,side,quantity,price,created_at) VALUES(?,?,?,?,?)',(t.symbol,t.side,t.quantity,price,int(time.time())))
    return {'ok':True,'execution_price':price,'paper_cash':cash,'note':'Simulated fill at last quote; not an executable market price'}
@app.get('/api/paper')
def paper():
    with connection() as db:
        cash=db.execute('SELECT cash FROM paper_account WHERE id=1').fetchone()['cash']
        trades=[dict(r) for r in db.execute('SELECT * FROM trades ORDER BY id DESC')]
        prices={r['symbol']:r['price'] for r in db.execute('SELECT * FROM quotes')}
    holdings={}
    for t in trades:holdings[t['symbol']]=holdings.get(t['symbol'],0)+(1 if t['side']=='buy' else -1)*t['quantity']
    return {'currency':'USD','cash':cash,'holdings':holdings,'trades':trades,'marked_value':cash+sum(q*prices[s] for s,q in holdings.items() if s in prices),'missing_marks':[s for s,q in holdings.items() if q>0 and s not in prices]}
@app.get('/',response_class=HTMLResponse)
def home():
    return '''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>InvestIQ</title><style>body{font:16px system-ui;background:#101827;color:#edf3ff;max-width:1100px;margin:auto;padding:22px}table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #334155;text-align:left}input,select,button{padding:10px;margin:5px;background:#253550;color:white;border:1px solid #52637b;border-radius:6px}button{cursor:pointer}.panel{background:#19253a;padding:16px;border-radius:12px;margin:14px 0}.warn{color:#ffcc77}</style></head><body><h1>AegisForge InvestIQ</h1><p class="warn">Research / paper trading only. Prices may be delayed. No real orders. USD paper cash; no EUR conversion.</p><div class="panel"><h2>Paper account</h2><pre id="account">Loading...</pre></div><div class="panel"><h2>Watchlist</h2><p id="updated"></p><div style="overflow:auto"><table><thead><tr><th>Symbol</th><th>USD price</th><th>Previous close</th><th>Market timestamp (UTC)</th><th>Fetch timestamp (UTC)</th></tr></thead><tbody id="quotes"></tbody></table></div></div><div class="panel"><h2>Create alert</h2><select id="symbol"></select><input id="above" type="number" step="any" min="0" placeholder="Above USD"><input id="below" type="number" step="any" min="0" placeholder="Below USD"><button onclick="addAlert()">Save alert</button><p id="alerts"></p></div><div class="panel"><h2>Paper trade</h2><select id="tradeSymbol"></select><select id="side"><option>buy</option><option>sell</option></select><input id="qty" type="number" step="0.001" min="0.001" placeholder="Shares"><button onclick="trade()">Simulate</button><p id="result"></p></div><script>const symbols='NVDA IONQ TSM MSFT AVGO MU CRWD RKLB OKLO CEG ASML VST ROK SOFI ENPH'.split(' ');for(const id of ['symbol','tradeSymbol'])document.getElementById(id).innerHTML=symbols.map(s=>'<option>'+s+'</option>').join('');const fmt=t=>t?new Date(t*1000).toISOString():'—';async function load(){try{let q=await(await fetch('/api/quotes')).json();document.getElementById('quotes').innerHTML=q.map(x=>'<tr><td>'+x.symbol+'</td><td>'+(x.price??'No data')+'</td><td>'+(x.previous_close??'—')+'</td><td>'+fmt(x.market_timestamp)+'</td><td>'+fmt(x.fetched_at)+'</td></tr>').join('');document.getElementById('updated').textContent='Dashboard refreshed: '+new Date().toLocaleString();document.getElementById('account').textContent=JSON.stringify(await(await fetch('/api/paper')).json(),null,2);document.getElementById('alerts').textContent=JSON.stringify(await(await fetch('/api/alerts')).json());}catch(e){document.getElementById('updated').textContent='Load failed: '+e}}async function addAlert(){let a={symbol:document.getElementById('symbol').value,above:Number(document.getElementById('above').value)||null,below:Number(document.getElementById('below').value)||null};let r=await fetch('/api/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(a)});alert(JSON.stringify(await r.json()));load()}async function trade(){let t={symbol:document.getElementById('tradeSymbol').value,side:document.getElementById('side').value,quantity:Number(document.getElementById('qty').value)};let r=await fetch('/api/paper/trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(t)});document.getElementById('result').textContent=JSON.stringify(await r.json());load()}load();setInterval(load,60000)</script></body></html>'''
