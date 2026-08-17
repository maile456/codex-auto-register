#!/opt/pay153/venv/bin/python
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from loguru import logger
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from paypal.flow import PayPalFlow
from paypal.graphql import GRIFFIN_METADATA_QUERY, CHECKOUT_SESSION_DATA_QUERY, SUPPORTED_FUNDING_SOURCES_QUERY
from paypal.models import UserInfo, CardInfo, BillingAddress, generate_card, generate_password
from paypal.proxy import ProxyConfig, ProxyEntry
from config import USER_AGENT

PROFILES={
 'CA':('en_CA','en','+1'), 'DE':('de_DE','de','+49'), 'FR':('fr_FR','fr','+33'),
 'ES':('es_ES','es','+34'), 'IT':('it_IT','it','+39'), 'NL':('nl_NL','nl','+31'),
 'BE':('nl_BE','nl','+32'), 'AT':('de_AT','de','+43'), 'CH':('de_CH','de','+41'),
 'IE':('en_IE','en','+353'), 'PT':('pt_PT','pt','+351'), 'NZ':('en_NZ','en','+64'),
 'SG':('en_SG','en','+65'), 'MY':('en_MY','en','+60'), 'HK':('en_HK','en','+852'),
 'KR':('ko_KR','ko','+82'), 'SE':('sv_SE','sv','+46'), 'NO':('no_NO','no','+47'),
 'DK':('da_DK','da','+45'), 'FI':('fi_FI','fi','+358'), 'PL':('pl_PL','pl','+48'),
 'CZ':('cs_CZ','cs','+420'), 'GR':('el_GR','el','+30'),
}
ADDRESSES={
 'CA':('Queen Street West','100','','Toronto','ON','M5H 2N2'),
 'DE':('Unter den Linden','1','','Berlin','BE','10117'),
 'FR':('Avenue des Champs-Elysees','10','','Paris','Ile-de-France','75008'),
 'ES':('Gran Via','28','','Madrid','Madrid','28013'),
 'IT':('Via del Corso','18','','Roma','RM','00186'),
 'NL':('Damrak','1','','Amsterdam','Noord-Holland','1012 LG'),
 'BE':('Rue de la Loi','16','','Bruxelles','Bruxelles-Capitale','1000'),
 'AT':('Karntner Strasse','1','','Wien','Wien','1010'),
 'CH':('Bahnhofstrasse','1','','Zurich','ZH','8001'),
 'IE':('O Connell Street Upper','1','','Dublin','Dublin','D01'),
 'PT':('Avenida da Liberdade','100','','Lisboa','Lisboa','1250-096'),
 'NZ':('Queen Street','1','','Auckland','Auckland','1010'),
 'SG':('Raffles Place','1','','Singapore','Singapore','048616'),
 'MY':('Jalan Ampang','1','','Kuala Lumpur','Kuala Lumpur','50450'),
 'HK':('Queens Road Central','1','','Central','Hong Kong',''),
 'KR':('Sejong-daero','110','','Seoul','Seoul','04524'),
 'SE':('Drottninggatan','1','','Stockholm','Stockholm','111 51'),
 'NO':('Karl Johans gate','1','','Oslo','Oslo','0154'),
 'DK':('Stroget','1','','Copenhagen','Capital Region','1160'),
 'FI':('Mannerheimintie','1','','Helsinki','Uusimaa','00100'),
 'PL':('Marszalkowska','1','','Warsaw','Mazowieckie','00-001'),
 'CZ':('Vaclavske namesti','1','','Prague','Prague','110 00'),
 'GR':('Ermou','1','','Athens','Attica','105 63'),
}
class ProbeFlow(PayPalFlow):
 def _handle_datadome_challenge(self,response,agreement_url):
  raise RuntimeError('DATADOME_403')
 def _normalize_address_with_paypal(self,token):
  return None

def summarize(value):
 text=json.dumps(value,ensure_ascii=False)
 keys=sorted(set(re.findall(r'"([A-Za-z][A-Za-z0-9_]{2,60})"\s*:',text)))
 focus=[k for k in keys if any(x in k.lower() for x in ('field','address','identity','national','birth','phone','postal','state','consent','middle','tax','document','name'))]
 strings=sorted(set(re.findall(r'"([^"\\]{3,100})"',text)))
 focus_strings=[s for s in strings if any(x in s.lower() for x in ('identity','national','birth','middle name','postal','postcode','state','province','suburb','consent','tax id','phone'))]
 return {'keys':focus[:300],'strings':focus_strings[:300]}

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('country'); ap.add_argument('--ba',required=True); ap.add_argument('--proxy',required=True); args=ap.parse_args()
 cc=args.country.upper(); locale,lang,phone_code=PROFILES[cc]; street,num,district,city,state,postal=ADDRESSES[cc]
 phone_local={'CA':'4165550123','DE':'15123456789','FR':'612345678','ES':'612345678','IT':'3123456789','NL':'612345678','BE':'470123456','AT':'6641234567','CH':'791234567','IE':'851234567','PT':'912345678','NZ':'211234567','SG':'81234567','MY':'123456789','HK':'51234567','KR':'1012345678','SE':'701234567','NO':'41234567','DK':'20123456','FI':'401234567','PL':'512345678','CZ':'601123456','GR':'6912345678'}[cc]
 user=UserInfo('Alex','Taylor',f'probe.{cc.lower()}@example.com',phone_code+phone_local,phone_local,phone_code,generate_password(),'01/01/1990','')
 address=BillingAddress(street,num,district,city,state,postal,cc)
 proxy=ProxyConfig(True,ProxyEntry.parse(args.proxy))
 flow=ProbeFlow(args.ba,user,generate_card(prefer_local=True),address,proxy_config=proxy)
 flow.profile={'locale':locale,'lang':lang,'phone_code':phone_code,'accept_language':locale.replace('_','-')+',en;q=0.8'}
 flow.locale=locale; flow.lang=lang; flow.phone_code=phone_code; flow.accept_language=flow.profile['accept_language']; flow.session.locale=locale; flow.session.country=cc
 out=ROOT/'data'/'country_discovery'/cc; out.mkdir(parents=True,exist_ok=True)
 logger.remove(); logger.add(sys.stderr,level='INFO')
 try:
  flow._phase0_initial_load(); flow._phase1_risk_controls(); flow._phase2_create_account()
  html=flow.session.get(flow.state.signup_url).text if flow.state.signup_url else ''
  (out/'signup.html').write_text(html,encoding='utf-8')
  results={}
  results['griffin']=flow.session.graphql('GriffinMetadataQuery',GRIFFIN_METADATA_QUERY,{'countryCode':cc,'languageCode':lang,'shippingCountryCode':cc})
  results['checkout']=flow.session.graphql('CheckoutSessionDataQuery',CHECKOUT_SESSION_DATA_QUERY,{'token':flow.state.ec_token or args.ba})
  results['funding']=flow.session.graphql('SupportedFundingSourcesQuery',SUPPORTED_FUNDING_SOURCES_QUERY,{'token':flow.state.ec_token or args.ba,'userCountry':cc})
  (out/'responses.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
  initial=flow._extract_window_initial_data(html)
  summary={'country':cc,'locale':locale,'ec':bool(flow.state.ec_token),'signup_url':flow.state.signup_url,'content_identifier':flow.state.content_identifier,'initial_keys':sorted(initial.keys()),'response_summary':summarize(results),'html_field_tokens':sorted(set(re.findall(r'(?:firstName|middleName|lastName|addressLine1|addressLine2|postalCode|postcode|suburb|province|state|identityDocument|nationality|dateOfBirth|taxId|phoneNumber)',html,re.I)))}
  # Render the real signup UI with the same proxy and cookie jar, then
  # extract visible controls/labels. This is authoritative for country fields.
  try:
   from playwright.sync_api import sync_playwright
   cookie_rows=[]
   for cookie in flow.session.client.cookies.jar:
    name=str(getattr(cookie,'name','') or ''); value=str(getattr(cookie,'value','') or '')
    if not name or not value: continue
    domain=str(getattr(cookie,'domain','') or '.paypal.com')
    if not domain.startswith('.'): domain='.'+domain
    cookie_rows.append({'name':name,'value':value,'domain':domain,'path':str(getattr(cookie,'path','') or '/'),'secure':bool(getattr(cookie,'secure',True))})
   with sync_playwright() as pw:
    entry=proxy.entry
    browser=pw.chromium.launch(headless=False,executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-dev-shm-usage'],proxy={'server':f'{entry.scheme}://{entry.host}:{entry.port}','username':entry.username,'password':entry.password})
    ctx=browser.new_context(locale=locale.replace('_','-'),user_agent=USER_AGENT,viewport={'width':430,'height':1200})
    if cookie_rows: ctx.add_cookies(cookie_rows)
    page=ctx.new_page(); page.goto(flow.state.signup_url,wait_until='domcontentloaded',timeout=45000); page.wait_for_timeout(6000)
    fields=page.locator('input, select, textarea, button').evaluate_all("""els => els.map(e => {
      const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
      return {tag:e.tagName.toLowerCase(),type:e.type||'',name:e.name||'',id:e.id||'',placeholder:e.placeholder||'',autocomplete:e.autocomplete||'',required:!!e.required,ariaLabel:e.getAttribute('aria-label')||'',text:(e.innerText||e.value||'').trim().slice(0,200),options:e.tagName==='SELECT'?Array.from(e.options).map(o=>({value:o.value,text:(o.textContent||'').trim()})):[],visible:r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
    }).filter(x=>x.visible)""")
    labels=page.locator('label, legend, h1, h2, h3').evaluate_all("""els => els.map(e => (e.innerText||'').trim()).filter(Boolean)""")
    dom={'url':page.url,'title':page.title(),'fields':fields,'labels':labels}
    (out/'dom_fields.json').write_text(json.dumps(dom,ensure_ascii=False,indent=2),encoding='utf-8')
    page.screenshot(path=str(out/'signup.png'),full_page=True)
    browser.close()
   summary['dom_fields']=fields; summary['dom_labels']=labels
  except Exception as browser_error:
   summary['browser_error']=str(browser_error)
  (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
  print(json.dumps(summary,ensure_ascii=False,indent=2))
 finally:
  flow.close()
if __name__=='__main__': main()
