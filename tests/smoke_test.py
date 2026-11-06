import sys; import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
print(c.get('/health').json())
r=c.get('/v1/chart',params={'datetime':'1947-08-14T18:30:00Z','lat':28.6139,'lon':77.209}).json()
print('ayan',r['ayanamsa_value'],'lagna',r['lagna']['sign_name'],round(r['lagna']['deg_in_sign'],3))
for k,v in r['planets'].items(): print(k,v['sign_name'],round(v['deg_in_sign'],3),v['nakshatra_name'],v['pada'],'R' if v['retro'] else '')
p=c.get('/v1/panchang',params={'date':'2026-11-06','lat':17.385,'lon':78.4867,'tz':'Asia/Kolkata'}).json()
print('sunrise',p['sunrise'],'sunset',p['sunset'],'moonrise',p['moonrise'])
for k,v in p['elements'].items(): print(k,[(x['name'],x['end']) for x in v])
print(c.get('/v1/chart',params={'datetime':'1990-01-15T01:00:00Z','lat':17.385,'lon':78.4867,'ayanamsa':'kp','node':'true'}).json()['planets']['Rahu']['lon'])
print(c.post('/v1/positions',json={'datetimes':['2026-11-08T12:00:00Z'],'bodies':['Sun','Moon']}).json())
print(c.get('/v1/chart',params={'datetime':'bad','lat':1,'lon':1}).status_code)
