#!/usr/bin/env python3
"""
DISCO — Companies House Enrichment Script
Runs as a GitHub Action. CH_API_KEY must be set as a GitHub Secret.
Writes enrichment.json to the repo root for DISCO to load on startup.
"""

import os
import json
import time
import requests
from datetime import datetime, timezone

CH_API_KEY = os.environ.get('CH_API_KEY', '')
CH_BASE    = 'https://api.company-information.service.gov.uk'
SESSION    = requests.Session()
SESSION.auth = (CH_API_KEY, '')          # Basic Auth: key as username, empty password
SESSION.headers.update({'Accept': 'application/json', 'User-Agent': 'DISCO-Enrichment/1.0'})

# ── All lead IDs and names from DISCO's LEADS_DB
# Keep this in sync with index.html LEADS_DB
LEADS = [
  {'id':'m1',  'name':'Apex Industrial Equipment Ltd'},
  {'id':'m2',  'name':'Britannia Machinery Imports Ltd'},
  {'id':'m3',  'name':'Croft Engineering Supplies Ltd'},
  {'id':'m4',  'name':'Delta Process Equipment Ltd'},
  {'id':'m5',  'name':'Euro Plant & Machinery Ltd'},
  {'id':'m6',  'name':'Falcon CNC Solutions Ltd'},
  {'id':'m7',  'name':'Global Hydraulics UK Ltd'},
  {'id':'m8',  'name':'Halcyon Lifting Equipment Ltd'},
  {'id':'m9',  'name':'Impex Manufacturing Solutions'},
  {'id':'m10', 'name':'Jarvis Precision Engineering Ltd'},
  {'id':'m11', 'name':'KMS Automation UK Ltd'},
  {'id':'m12', 'name':'Mercer Industrial Systems Ltd'},
  {'id':'m13', 'name':'Norden Packaging Machinery Ltd'},
  {'id':'m14', 'name':'Dextra Robotic Systems Ltd'},
  {'id':'m15', 'name':'Westfield Industrial Pumps Ltd'},
  {'id':'m16', 'name':'Xcel Automation Systems Ltd'},
  {'id':'t1',  'name':'Aldgate Fabric Importers Ltd'},
  {'id':'t2',  'name':'Burlington Yarn & Textiles Ltd'},
  {'id':'t3',  'name':'Denim World Imports Ltd'},
  {'id':'t4',  'name':'European Wool Brokers Ltd'},
  {'id':'f1',  'name':'Continental Foods UK Ltd'},
  {'id':'f2',  'name':'Brio Coffee Importers Ltd'},
  {'id':'f3',  'name':'Global Grain Brokers Ltd'},
  {'id':'a1',  'name':'British Aerospace Supplies Ltd'},
  {'id':'a2',  'name':'Delta Defence Systems Ltd'},
  {'id':'c1',  'name':'Anglo Excavator Imports Ltd'},
  {'id':'c2',  'name':'Bulldozer Direct UK Ltd'},
  {'id':'c3',  'name':'European Plant UK Ltd'},
]

FINANCE_ROLES = [
  'chief financial officer', 'cfo', 'group finance director',
  'finance director', 'financial director', 'director of finance',
  'commercial finance director', 'head of finance',
  'financial controller', 'finance manager',
]

def role_rank(role):
  r = (role or '').lower()
  if 'chief financial' in r or ' cfo' in r: return 10
  if 'group finance director' in r or 'group cfo' in r: return 9
  if 'finance director' in r or 'financial director' in r: return 8
  if 'director of finance' in r: return 8
  if 'head of finance' in r: return 7
  if 'financial controller' in r: return 6
  if 'finance manager' in r: return 5
  return 0

def is_finance_role(role):
  return role_rank(role) > 0

def ch_get(path, params=None):
  url = f'{CH_BASE}{path}'
  try:
    r = SESSION.get(url, params=params, timeout=10)
    if r.status_code == 429:
      print(f'  Rate limited — waiting 60s')
      time.sleep(60)
      r = SESSION.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()
  except Exception as e:
    print(f'  CH request failed: {path} — {e}')
    return None

def search_company(name):
  data = ch_get('/search/companies', {'q': name, 'items_per_page': 5})
  if not data or not data.get('items'):
    return None
  # Try exact match first, then first result
  items = data['items']
  for item in items:
    if item.get('title', '').lower() == name.lower():
      return item
  return items[0]

def get_officers(company_number):
  data = ch_get(f'/company/{company_number}/officers', {'items_per_page': 50})
  if not data:
    return []
  return [o for o in data.get('items', []) if not o.get('resigned_on')]

def format_ch_name(name):
  """CH returns 'SURNAME, Firstname Middle' — reformat to 'Firstname Surname'"""
  if ',' in name:
    parts = name.split(',', 1)
    surname = parts[0].strip().title()
    forenames = parts[1].strip().title()
    return f'{forenames} {surname}'
  return name.title()

def extract_finance_contact(officers):
  """Find best finance contact from officer list"""
  best = None
  best_rank = 0
  for o in officers:
    role = o.get('officer_role', '')
    occupation = o.get('occupation', '')
    rk = role_rank(role) or role_rank(occupation)
    if rk > best_rank:
      best_rank = rk
      best = o
  if best and best_rank >= 5:
    name = format_ch_name(best.get('name', ''))
    role = best.get('occupation') or best.get('officer_role', 'Finance Director')
    appointed = best.get('appointed_on', '')
    appointed_year = int(appointed[:4]) if appointed and len(appointed) >= 4 else None
    return {
      'fd': name,
      'fdRole': role.title() if role else 'Finance Director',
      'appointed': appointed_year,
      'appointedDate': appointed,
      'contactSource': 'CH_OFFICERS',
      'confidence': min(0.95, 0.7 + best_rank * 0.03),
    }
  return None

def enrich_lead(lead):
  name = lead['name']
  print(f'  Enriching: {name}')
  result = {
    'id': lead['id'],
    'enrichedAt': datetime.now(timezone.utc).isoformat(),
    'status': 'PARTIAL',
    'companyNumber': None,
    'companyStatus': None,
    'sicCodes': [],
    'fd': None,
    'fdRole': None,
    'appointed': None,
    'contactSource': None,
    'confidence': 0,
    'officers': [],
    'sources': [],
    'fxSignals': [],
    'triggerEvents': [],
  }

  # Step 1: search for company
  company = search_company(name)
  if not company:
    print(f'    Not found in CH')
    result['status'] = 'FAILED'
    return result

  company_number = company.get('company_number')
  result['companyNumber'] = company_number
  result['companyStatus'] = company.get('company_status', '')
  result['sources'].append({
    'sourceName': 'Companies House search',
    'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{company_number}',
    'retrievedAt': result['enrichedAt'],
  })
  print(f'    Found: {company_number} ({company.get("company_status","")})')
  time.sleep(0.3)  # polite rate limiting

  # Step 2: company profile for SIC codes
  profile = ch_get(f'/company/{company_number}')
  if profile:
    sic_codes = profile.get('sic_codes', [])
    result['sicCodes'] = sic_codes
    # Registered office
    ro = profile.get('registered_office_address', {})
    result['registeredOffice'] = ', '.join(filter(None, [
      ro.get('address_line_1'), ro.get('locality'), ro.get('postal_code')
    ]))
  time.sleep(0.3)

  # Step 3: officers
  officers = get_officers(company_number)
  result['sources'].append({
    'sourceName': 'Companies House officers',
    'sourceUrl': f'https://find-and-update.company-information.service.gov.uk/company/{company_number}/officers',
    'retrievedAt': result['enrichedAt'],
  })
  time.sleep(0.3)

  # Format officer list for display
  result['officers'] = [{
    'name': format_ch_name(o.get('name', '')),
    'role': o.get('officer_role', ''),
    'occupation': o.get('occupation', ''),
    'appointed': o.get('appointed_on', ''),
  } for o in officers[:10]]

  # Step 4: find best finance contact
  contact = extract_finance_contact(officers)
  if contact:
    result.update(contact)
    print(f'    Finance contact: {contact["fd"]} ({contact["fdRole"]})')
  else:
    print(f'    No finance contact found in officers')

  result['status'] = 'ENRICHED' if contact else 'PARTIAL'
  return result

def main():
  if not CH_API_KEY:
    print('ERROR: CH_API_KEY environment variable not set')
    print('Add it as a GitHub Secret: Settings → Secrets → Actions → CH_API_KEY')
    exit(1)

  print(f'DISCO Enrichment — {datetime.now().strftime("%Y-%m-%d %H:%M")}')
  print(f'Enriching {len(LEADS)} leads...\n')

  results = {}
  for i, lead in enumerate(LEADS):
    result = enrich_lead(lead)
    results[lead['id']] = result
    # Polite rate limiting — CH allows ~600 req/min on free tier
    if i < len(LEADS) - 1:
      time.sleep(0.5)

  # Write output
  output = {
    'generatedAt': datetime.now(timezone.utc).isoformat(),
    'leadCount': len(results),
    'enriched': sum(1 for r in results.values() if r['status'] == 'ENRICHED'),
    'partial': sum(1 for r in results.values() if r['status'] == 'PARTIAL'),
    'failed': sum(1 for r in results.values() if r['status'] == 'FAILED'),
    'leads': results,
  }

  with open('enrichment.json', 'w') as f:
    json.dump(output, f, indent=2)

  print(f'\nDone: {output["enriched"]} enriched, {output["partial"]} partial, {output["failed"]} failed')
  print(f'Written to enrichment.json')

if __name__ == '__main__':
  main()
