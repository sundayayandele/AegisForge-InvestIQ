import os,tempfile
os.environ['ENABLE_SCHEDULER']='0'
os.environ['DB_PATH']=tempfile.mktemp(suffix='.db')
from fastapi.testclient import TestClient
from app.main import app,init,connection
init()
client=TestClient(app)
def test_watchlist():
    r=client.get('/api/quotes');assert r.status_code==200 and len(r.json())==15
    assert all(x['price'] is None for x in r.json())
def test_alert_validation():
    assert client.post('/api/alerts',json={'symbol':'FAKE','above':10}).status_code==400
    assert client.post('/api/alerts',json={'symbol':'NVDA','above':10}).status_code==200
def test_paper_no_quote():
    assert client.post('/api/paper/trade',json={'symbol':'NVDA','side':'buy','quantity':1}).status_code==409
def test_paper_buy_sell():
    import time
    with connection() as db:db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?)',('NVDA',100,99,int(time.time()),int(time.time())))
    r=client.post('/api/paper/trade',json={'symbol':'NVDA','side':'buy','quantity':2});assert r.status_code==200
    assert r.json()['paper_cash']==800
    assert client.post('/api/paper/trade',json={'symbol':'NVDA','side':'sell','quantity':3}).status_code==400
    assert client.post('/api/paper/trade',json={'symbol':'NVDA','side':'sell','quantity':2}).status_code==200
    assert client.get('/api/paper').json()['cash']==1000
