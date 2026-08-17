#!/opt/pay153/venv/bin/python
from __future__ import annotations
import json,random,string,time,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from paypal.graphql import GRIFFIN_METADATA_QUERY
from paypal.models import SessionState
from paypal.proxy import ProxyEntry
from paypal.session import PayPalSession
PROFILES={
 'CA':('en_CA','en'),'DE':('de_DE','de'),'FR':('fr_FR','fr'),'ES':('es_ES','es'),'IT':('it_IT','it'),'NL':('nl_NL','nl'),'BE':('nl_BE','nl'),'AT':('de_AT','de'),'CH':('de_CH','de'),'IE':('en_IE','en'),'PT':('pt_PT','pt'),'NZ':('en_NZ','en'),'SG':('en_SG','en'),'MY':('en_MY','en'),'HK':('en_HK','en'),'KR':('ko_KR','ko'),'SE':('sv_SE','sv'),'NO':('no_NO','no'),'DK':('da_DK','da'),'FI':('fi_FI','fi'),'PL':('pl_PL','pl'),'CZ':('cs_CZ','cs'),'GR':('el_GR','el'),'AR':('es_AR','es'),'CL':('es_CL','es'),'CO':('es_CO','es'),'PE':('es_PE','es'),'ZA':('en_ZA','en'),'SA':('ar_SA','ar'),'IL':('he_IL','he'),'TR':('tr_TR','tr')}

def proxy_for(cc):
 sid=''.join(random.choice(string.ascii_letters+string.digits) for _ in range(8))
 return f'us.1024proxy.io:3000:xwic52988-region-{cc}-sid-{sid}-t-5:9qoeaajv'

def one(cc,locale,lang):
 last=''
 for _ in range(6):
  sess=None
  try:
   entry=ProxyEntry.parse(proxy_for(cc)); state=SessionState(); sess=PayPalSession(state,proxy_url=entry.url,proxy_label=entry.masked,country=cc,locale=locale)
   result=sess.graphql('GriffinMetadataQuery',GRIFFIN_METADATA_QUERY,{'countryCode':cc,'languageCode':lang,'shippingCountryCode':cc})
   obj=result[0] if isinstance(result,list) else result
   meta=(obj.get('data') or {}).get('localeMetadata') or {}
   address=(meta.get('address') or {}).get('layout') or []
   phone=meta.get('phone') or {}
   return {'country':cc,'locale':locale,'language':lang,'currency':meta.get('currencyCode'),'address':address,'phone':phone}
  except Exception as e: last=str(e)
  finally:
   if sess:
    try:sess.close()
    except:pass
 return {'country':cc,'locale':locale,'language':lang,'error':last}

out={}
for cc,(locale,lang) in PROFILES.items():
 out[cc]=one(cc,locale,lang); print(cc,'ok' if 'error' not in out[cc] else 'error',flush=True)
p=Path('/opt/paypal-pay/data/country_discovery/locale_metadata.json'); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
